"""
indexer.py — Indexa el codebase Go y el knowledge (.md) en ChromaDB (Nivel 4).

- Recorre *.go de config.REPOS (excluye vendor/).
- Divide cada archivo por bloque funcional (func / method / type / interface)
  para búsqueda granular; cada chunk lleva la línea de package como contexto.
- Guarda embeddings con metadata: repo, archivo, línea, tipo, nombre, y desde
  el indexado incremental: blob_sha (hash git del contenido), commit (HEAD del
  repo al indexar) e indexed_at (UTC).

Indexado incremental (only_changed=True, default):
- Un archivo se reindexa solo si su blob_sha difiere del guardado en ChromaDB.
- Antes de cada upsert se borran TODOS los chunks previos de ese archivo: los
  ids llevan la línea de inicio, así que sin este borrado un bloque que se
  mueve de línea dejaba su chunk viejo vivo para siempre.
- Al final se purgan los chunks de archivos que ya no existen en disco.
- Con only_changed=False se reescriben todos los archivos (reconstrucción).

chromadb se importa perezoso vía chroma_client (solo con RAG activo), para
que el arranque del agente no dependa de tener chromadb instalado.
"""

import hashlib
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from minka.core.tools.rag.chroma_client import get_client

CHROMA_COLLECTION = "codebase"
KNOWLEDGE_COLLECTION = "knowledge_docs"

# Patrones de bloques funcionales Go (orden importa: método antes que type suelto)
_PATTERNS = [
    (r"^func\s+(\w+)", "func"),
    (r"^func\s+\(.*?\)\s+(\w+)", "method"),
    (r"^type\s+(\w+)\s+interface", "interface"),
    (r"^type\s+(\w+)\s+struct", "struct"),
    (r"^type\s+(\w+)\s+", "type"),
]


def get_or_create_collection(client, name: str = CHROMA_COLLECTION):
    return client.get_or_create_collection(
        name=name,
        metadata={"hnsw:space": "cosine"},
    )


def chunk_go_file(content: str, filepath: Path) -> list[dict]:
    """
    Divide un archivo Go en chunks funcionales:
    - Por función (func NombreFuncion)
    - Por método (func (r *X) Nombre)
    - Por tipo/struct/interface (type Nombre ...)
    Cada chunk incluye la línea de package como contexto.
    """
    chunks = []
    lines = content.split("\n")

    package_line = next((ln for ln in lines if ln.startswith("package ")), "")

    current_chunk = []
    current_name = None
    current_type = None
    start_line = 0

    for i, line in enumerate(lines):
        for pattern, block_type in _PATTERNS:
            match = re.match(pattern, line)
            if match:
                if current_chunk and current_name:
                    chunks.append(
                        {
                            "content": f"{package_line}\n\n" + "\n".join(current_chunk),
                            "name": current_name,
                            "type": current_type,
                            "file": str(filepath),
                            "start_line": start_line + 1,
                        }
                    )
                current_chunk = [line]
                current_name = match.group(1)
                current_type = block_type
                start_line = i
                break
        else:
            if current_chunk:
                current_chunk.append(line)

    if current_chunk and current_name:
        chunks.append(
            {
                "content": f"{package_line}\n\n" + "\n".join(current_chunk),
                "name": current_name,
                "type": current_type,
                "file": str(filepath),
                "start_line": start_line + 1,
            }
        )

    # Si no encontró bloques, indexa el archivo completo (acotado)
    if not chunks:
        chunks.append(
            {
                "content": content[:2000],
                "name": filepath.stem,
                "type": "file",
                "file": str(filepath),
                "start_line": 1,
            }
        )

    return chunks


def chunk_md_file(content: str, filepath: Path) -> list[dict]:
    """
    Divide un archivo Markdown en chunks por sección (encabezados #..######).
    Cada chunk agrupa el texto desde su encabezado hasta el siguiente y lleva
    el título de la sección como contexto. Si no hay encabezados, indexa el
    archivo completo (acotado), igual que chunk_go_file.
    """
    chunks = []
    lines = content.split("\n")

    current_lines = []
    current_title = None
    start_line = 0

    for i, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+)", line)
        if match:
            if current_lines and any(ln.strip() for ln in current_lines):
                chunks.append(
                    {
                        "content": "\n".join(current_lines).strip(),
                        "section": current_title or filepath.stem,
                        "file": str(filepath),
                        "start_line": start_line + 1,
                    }
                )
            current_lines = [line]
            current_title = match.group(2).strip()
            start_line = i
        else:
            current_lines.append(line)

    if current_lines and any(ln.strip() for ln in current_lines):
        chunks.append(
            {
                "content": "\n".join(current_lines).strip(),
                "section": current_title or filepath.stem,
                "file": str(filepath),
                "start_line": start_line + 1,
            }
        )

    # Sin encabezados: indexa el archivo completo (acotado).
    if not chunks:
        chunks.append(
            {
                "content": content[:2000],
                "section": filepath.stem,
                "file": str(filepath),
                "start_line": 1,
            }
        )

    return chunks


# ---------------------------------------------------------------------------
# Frescura: hash de contenido, commit y utilidades de reemplazo/purga
# ---------------------------------------------------------------------------


def blob_sha(content: bytes) -> str:
    """SHA-1 de blob git del contenido — idéntico a `git hash-object <archivo>`.
    Permite comparar lo indexado contra el working tree sin invocar git."""
    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()  # noqa: S324 — identidad git, no criptografía


def git_head(path: Path) -> str:
    """Commit HEAD (12 chars) del repo que contiene `path`. "" si no es un repo
    git o git no está disponible. Best-effort: nunca lanza."""
    try:
        res = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--short=12", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _indexed_files(collection, where: dict) -> dict[str, str]:
    """{ruta_archivo: blob_sha} de lo ya indexado bajo `where` (un repo o un
    source). Chunks indexados antes de esta versión no tienen blob_sha → ""
    y por tanto se reindexan en la primera corrida incremental."""
    data = collection.get(where=where, include=["metadatas"])
    indexed: dict[str, str] = {}
    for meta in data["metadatas"]:
        indexed.setdefault(meta["file"], meta.get("blob_sha", ""))
    return indexed


def _replace_file_chunks(collection, file_path: str, already_indexed: bool, **batch) -> None:
    """Borra los chunks previos del archivo (si los hay) y sube los nuevos.
    El borrado previo es lo que elimina chunks de bloques movidos/renombrados."""
    if already_indexed:
        collection.delete(where={"file": file_path})
    collection.upsert(**batch)


def _purge_missing(collection, indexed: dict[str, str], on_disk: set[str]) -> int:
    """Borra los chunks de archivos indexados que ya no existen en disco.
    Retorna cuántos archivos se purgaron."""
    missing = [f for f in indexed if f not in on_disk]
    for file_path in missing:
        collection.delete(where={"file": file_path})
    return len(missing)


# ---------------------------------------------------------------------------
# Indexado
# ---------------------------------------------------------------------------


def index_repos(config, only_changed: bool = True) -> dict:
    """
    Indexa los archivos .go de los repos configurados (incremental por default).
    Retorna estadísticas: archivos reescritos, chunks, saltados (sin cambios),
    purgados (ya no existen), errores y desglose por repo con su commit HEAD.
    Upsert por lotes (una llamada por archivo) para no invocar el embedder
    chunk por chunk.
    """
    if not config.RAG_ENABLED:
        return {"error": "RAG_ENABLED=False en config"}

    client = get_client()
    collection = get_or_create_collection(client)

    stats = {"files": 0, "chunks": 0, "skipped": 0, "purged": 0, "errors": 0, "repos": {}}

    for repo_name, repo_path in config.REPOS.items():
        repo_root = Path(repo_path)
        if not repo_root.exists():
            stats["errors"] += 1
            stats["repos"][repo_name] = "ERROR: ruta no existe"
            continue

        commit = git_head(repo_root)
        indexed_at = _now_iso()
        indexed = _indexed_files(collection, {"repo": repo_name})
        repo_stats = {"files": 0, "chunks": 0, "skipped": 0, "purged": 0, "commit": commit}

        go_files = [f for f in repo_root.rglob("*.go") if "vendor" not in f.parts]
        on_disk = {str(f) for f in go_files}

        for go_file in go_files:
            try:
                raw = go_file.read_bytes()
                sha = blob_sha(raw)
                file_key = str(go_file)
                if only_changed and indexed.get(file_key) == sha:
                    repo_stats["skipped"] += 1
                    continue

                content = raw.decode("utf-8", errors="ignore")
                chunks = chunk_go_file(content, go_file)
                if not chunks:
                    continue
                _replace_file_chunks(
                    collection,
                    file_key,
                    already_indexed=file_key in indexed,
                    documents=[c["content"] for c in chunks],
                    metadatas=[
                        {
                            "repo": repo_name,
                            "file": c["file"],
                            "name": c["name"],
                            "type": c["type"],
                            "start_line": c["start_line"],
                            "blob_sha": sha,
                            "commit": commit,
                            "indexed_at": indexed_at,
                        }
                        for c in chunks
                    ],
                    ids=[f"{repo_name}:{go_file}:{c['start_line']}" for c in chunks],
                )
                repo_stats["files"] += 1
                repo_stats["chunks"] += len(chunks)
            except Exception:
                stats["errors"] += 1

        repo_stats["purged"] = _purge_missing(collection, indexed, on_disk)

        for key in ("files", "chunks", "skipped", "purged"):
            stats[key] += repo_stats[key]
        stats["repos"][repo_name] = repo_stats

    return stats


def index_knowledge(config, only_changed: bool = True) -> dict:
    """
    Indexa los .md de conocimiento en la colección `knowledge_docs`
    (incremental por default, misma semántica que index_repos):
      - core/knowledge/            (genérico)                → source="generic"
      - projects/<proj>/knowledge/ (autodetectado, excluye
        _template)                                           → source="<proyecto>"
    Chunkea por sección Markdown. Upsert por archivo (un batch por archivo).
    Retorna stats: archivos, chunks, saltados, purgados, errores y desglose
    por source.
    """
    if not config.RAG_ENABLED:
        return {"error": "RAG_ENABLED=False en config"}

    client = get_client()
    collection = get_or_create_collection(client, KNOWLEDGE_COLLECTION)

    stats = {"files": 0, "chunks": 0, "skipped": 0, "purged": 0, "errors": 0, "sources": {}}

    # (source, ruta_raiz) — genérico + un knowledge/ por proyecto detectado.
    roots: list[tuple[str, Path]] = [("generic", Path(config.KNOWLEDGE_DIR))]

    projects_dir = Path(config.PROJECTS_DIR)
    if projects_dir.is_dir():
        for proj in sorted(projects_dir.iterdir()):
            if proj.name == "_template" or not proj.is_dir():
                continue
            knowledge_dir = proj / "knowledge"
            if knowledge_dir.is_dir():
                roots.append((proj.name, knowledge_dir))

    for source, root in roots:
        if not root.exists():
            stats["errors"] += 1
            stats["sources"][source] = "ERROR: ruta no existe"
            continue

        commit = git_head(root)
        indexed_at = _now_iso()
        indexed = _indexed_files(collection, {"source": source})
        source_stats = {"files": 0, "chunks": 0, "skipped": 0, "purged": 0, "commit": commit}

        md_files = list(root.rglob("*.md"))
        on_disk = {str(f) for f in md_files}

        for md_file in md_files:
            try:
                raw = md_file.read_bytes()
                sha = blob_sha(raw)
                file_key = str(md_file)
                if only_changed and indexed.get(file_key) == sha:
                    source_stats["skipped"] += 1
                    continue

                content = raw.decode("utf-8", errors="ignore")
                chunks = chunk_md_file(content, md_file)
                if not chunks:
                    continue
                _replace_file_chunks(
                    collection,
                    file_key,
                    already_indexed=file_key in indexed,
                    documents=[c["content"] for c in chunks],
                    metadatas=[
                        {
                            "source": source,
                            "file": c["file"],
                            "section": c["section"],
                            "type": "knowledge",
                            "start_line": c["start_line"],
                            "blob_sha": sha,
                            "commit": commit,
                            "indexed_at": indexed_at,
                        }
                        for c in chunks
                    ],
                    ids=[f"{source}:{md_file}:{c['start_line']}" for c in chunks],
                )
                source_stats["files"] += 1
                source_stats["chunks"] += len(chunks)
            except Exception:
                stats["errors"] += 1

        source_stats["purged"] = _purge_missing(collection, indexed, on_disk)

        for key in ("files", "chunks", "skipped", "purged"):
            stats[key] += source_stats[key]
        stats["sources"][source] = source_stats

    return stats


def get_index_stats(config) -> dict:
    """Retorna estadísticas del índice actual."""
    try:
        client = get_client()
        collection = get_or_create_collection(client)
        return {"total_chunks": collection.count(), "status": "ok"}
    except Exception as e:
        return {"error": str(e)}
