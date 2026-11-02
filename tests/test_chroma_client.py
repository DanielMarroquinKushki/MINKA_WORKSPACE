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


@pytest.mark.parametrize(
    "direct_url, configurado, origen",
    [
        (
            {"url": "https://git.example/org/minka.git", "vcs_info": {"vcs": "git"}},
            "",
            "git+https://git.example/org/minka.git",
        ),
        (None, "git+https://otro.example/minka.git", "git+https://otro.example/minka.git"),
        (None, "", "git+<url-del-repo>"),
    ],
)
def test_mensaje_rag_con_origen_de_instalacion(monkeypatch, direct_url, configurado, origen):
    from minka import _app

    monkeypatch.setattr(config, "RAG_ENABLED", True)
    monkeypatch.setattr(config, "INSTALL_SOURCE", configurado)
    monkeypatch.setattr(_app, "_direct_url", lambda: direct_url)
    monkeypatch.setitem(sys.modules, "chromadb", None)
    with pytest.raises(RuntimeError) as exc:
        chroma_client.get_client()
    assert f"uv tool install --reinstall 'minka-workspace[rag] @ {origen}'" in str(exc.value)
