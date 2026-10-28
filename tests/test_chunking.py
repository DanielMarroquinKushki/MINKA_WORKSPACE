"""Tests del chunking del RAG (chunk_md_file / chunk_go_file)."""
from pathlib import Path

from core.tools.rag.indexer import chunk_go_file, chunk_md_file


def test_chunk_md_by_headers():
    md = "# Titulo\nintro\n## Seccion A\ncontenido A\n## Seccion B\ncontenido B\n"
    chunks = chunk_md_file(md, Path("doc.md"))
    sections = [c["section"] for c in chunks]
    assert "Titulo" in sections
    assert "Seccion A" in sections
    assert "Seccion B" in sections


def test_chunk_md_no_headers_fallback():
    chunks = chunk_md_file("solo texto sin encabezados", Path("plano.md"))
    assert len(chunks) == 1
    assert chunks[0]["section"] == "plano"


def test_chunk_go_functions_and_types():
    go = "package main\n\nfunc Foo() {\n  return\n}\n\ntype Bar struct {\n  X int\n}\n"
    chunks = chunk_go_file(go, Path("x.go"))
    names = [c["name"] for c in chunks]
    assert "Foo" in names
    assert "Bar" in names
    # cada chunk lleva la línea package como contexto
    assert all(c["content"].startswith("package main") for c in chunks)