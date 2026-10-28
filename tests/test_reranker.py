"""Tests del fallback del reranker (sin modelo → orden original).

El rerank real con modelo NO se testea aquí (descargaría ~2GB): se valida a mano.
Aquí bloqueamos el contrato de degradación: si el modelo no está, nunca crashea
y devuelve el orden original con score None.
"""
from core.tools.rag import reranker


def test_rerank_empty_returns_empty():
    assert reranker.rerank("q", [], 3) == []


def test_rerank_fallback_keeps_order_when_no_model(monkeypatch):
    monkeypatch.setattr(reranker, "_get_model", lambda: None)
    out = reranker.rerank("q", ["a", "b", "c", "d"], top_n=2)
    assert out == [(0, None), (1, None)]


def test_rerank_fallback_on_predict_error(monkeypatch):
    class BadModel:
        def predict(self, pairs):
            raise RuntimeError("boom")

    monkeypatch.setattr(reranker, "_get_model", lambda: BadModel())
    out = reranker.rerank("q", ["a", "b", "c"], top_n=3)
    assert out == [(0, None), (1, None), (2, None)]