"""Sin el extra rag, get_client() explica qué instalar en vez de un ModuleNotFoundError."""

import sys

import pytest

from cincha.config_loader import config
from cincha.core.tools.rag import chroma_client


def test_chroma_sin_extra_rag_indica_el_extra(monkeypatch):
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setitem(sys.modules, "chromadb", None)  # simula chromadb no instalado
    with pytest.raises(RuntimeError, match=r"cincha\[rag\]"):
        chroma_client.get_client()
