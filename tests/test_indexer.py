"""Tests del indexado incremental del RAG (index_repos / index_knowledge).

Usan una colección falsa en memoria que imita el subconjunto de la API de
ChromaDB que usa el indexer (get/upsert/delete con `where` por igualdad), así
no dependen de chromadb ni del embedder.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from minka.core.tools.rag import indexer
from minka.core.tools.rag.indexer import blob_sha, index_knowledge, index_repos


class FakeCollection:
    """Imita get(where)/upsert/delete(where) de ChromaDB sobre un dict en memoria."""

    def __init__(self):
        self.docs: dict[str, tuple[str, dict]] = {}  # id -> (document, metadata)
        self.deletes: list[dict] = []

    @staticmethod
    def _match(meta: dict, where: dict | None) -> bool:
        return not where or all(meta.get(k) == v for k, v in where.items())

    def get(self, where=None, include=None, **_):
        ids = [i for i, (_, m) in self.docs.items() if self._match(m, where)]
        return {"ids": ids, "metadatas": [self.docs[i][1] for i in ids]}

    def upsert(self, documents, metadatas, ids):
        for doc, meta, id_ in zip(documents, metadatas, ids, strict=True):
            self.docs[id_] = (doc, meta)

    def delete(self, where=None, **_):
        self.deletes.append(where)
        for id_ in [i for i, (_, m) in self.docs.items() if self._match(m, where)]:
            del self.docs[id_]

    def files(self) -> set[str]:
        return {m["file"] for _, m in self.docs.values()}


@pytest.fixture
def fake_chroma(monkeypatch):
    """Sustituye el cliente Chroma por una colección falsa compartida."""
    collection = FakeCollection()
    monkeypatch.setattr(indexer, "get_client", lambda: object())
    monkeypatch.setattr(indexer, "get_or_create_collection", lambda _c, name=None: collection)
    monkeypatch.setattr(indexer, "git_head", lambda _p: "abc123def456")
    return collection


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "usrv-demo"
    repo.mkdir()
    (repo / "a.go").write_text("package a\n\nfunc Foo() {}\n", encoding="utf-8")
    (repo / "b.go").write_text("package a\n\nfunc Bar() {}\n\ntype T struct{}\n", encoding="utf-8")
    vendor = repo / "vendor" / "x"
    vendor.mkdir(parents=True)
    (vendor / "v.go").write_text("package x\n\nfunc Vendored() {}\n", encoding="utf-8")
    return repo


def _config(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(RAG_ENABLED=True, REPOS={"demo": str(repo)})


def test_blob_sha_matches_git_hash_object():
    # `git hash-object` de un archivo vacío es un valor conocido de git.
    assert blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert blob_sha(b"hola\n") != blob_sha(b"hola")


def test_first_run_indexes_everything_with_freshness_metadata(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    stats = index_repos(_config(repo))

    assert stats["files"] == 2 and stats["skipped"] == 0 and stats["errors"] == 0
    assert stats["repos"]["demo"]["commit"] == "abc123def456"
    assert fake_chroma.files() == {str(repo / "a.go"), str(repo / "b.go")}  # vendor excluido
    meta = next(iter(fake_chroma.docs.values()))[1]
    assert meta["blob_sha"] == blob_sha((Path(meta["file"])).read_bytes())
    assert meta["commit"] == "abc123def456"
    assert meta["indexed_at"]


def test_second_run_skips_unchanged_files(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    index_repos(_config(repo))
    stats = index_repos(_config(repo))

    assert stats["files"] == 0
    assert stats["skipped"] == 2
    assert stats["purged"] == 0


def test_changed_file_is_replaced_not_accumulated(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    index_repos(_config(repo))
    ids_before = {i for i in fake_chroma.docs if i.endswith("a.go:3")}
    assert ids_before  # Foo empezaba en la línea 3

    # Foo se mueve de línea: sin borrado previo, el chunk viejo quedaría huérfano.
    (repo / "a.go").write_text("package a\n\n// comentario\n\nfunc Foo() {}\n", encoding="utf-8")
    stats = index_repos(_config(repo))

    assert stats["files"] == 1 and stats["skipped"] == 1
    a_chunks = [i for i, (_, m) in fake_chroma.docs.items() if m["file"].endswith("a.go")]
    assert len(a_chunks) == 1 and a_chunks[0].endswith("a.go:5")
    assert {"file": str(repo / "a.go")} in fake_chroma.deletes


def test_deleted_file_is_purged(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    index_repos(_config(repo))
    (repo / "b.go").unlink()

    stats = index_repos(_config(repo))

    assert stats["purged"] == 1
    assert fake_chroma.files() == {str(repo / "a.go")}


def test_full_reindex_rewrites_unchanged_files(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    index_repos(_config(repo))
    stats = index_repos(_config(repo), only_changed=False)

    assert stats["files"] == 2 and stats["skipped"] == 0


def test_rag_disabled_returns_error_without_touching_chroma(fake_chroma, tmp_path):
    repo = _repo(tmp_path)
    stats = index_repos(SimpleNamespace(RAG_ENABLED=False, REPOS={"demo": str(repo)}))
    assert "error" in stats and not fake_chroma.docs


def test_index_knowledge_incremental_and_purge(fake_chroma, tmp_path):
    generic = tmp_path / "core_knowledge"
    generic.mkdir()
    (generic / "guide.md").write_text("# Guía\ntexto\n## Sección\nmás\n", encoding="utf-8")
    proj = tmp_path / "projects" / "demo" / "knowledge"
    proj.mkdir(parents=True)
    (proj / "notes.md").write_text("# Notas\nalgo\n", encoding="utf-8")
    (tmp_path / "projects" / "_template" / "knowledge").mkdir(parents=True)
    cfg = SimpleNamespace(
        RAG_ENABLED=True, KNOWLEDGE_DIR=generic, PROJECTS_DIR=tmp_path / "projects"
    )

    first = index_knowledge(cfg)
    assert first["files"] == 2 and set(first["sources"]) == {"generic", "demo"}
    assert all(m["source"] in {"generic", "demo"} for _, m in fake_chroma.docs.values())

    (proj / "notes.md").unlink()
    second = index_knowledge(cfg)
    assert second["skipped"] == 1 and second["purged"] == 1
    assert fake_chroma.files() == {str(generic / "guide.md")}
