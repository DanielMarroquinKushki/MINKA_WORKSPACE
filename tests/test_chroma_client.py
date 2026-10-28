"""Sin el extra rag, get_client() explica qué instalar en vez de un ModuleNotFoundError."""

import sys

import pytest

from minka.config_loader import config
from minka.core.tools.rag import chroma_client


def test_chroma_sin_extra_rag_indica_el_extra(monkeypatch):
    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setitem(sys.modules, "chromadb", None)  # simula chromadb no instalado
    with pytest.raises(RuntimeError, match=r"minka-workspace\[rag\]"):
        chroma_client.get_client()
