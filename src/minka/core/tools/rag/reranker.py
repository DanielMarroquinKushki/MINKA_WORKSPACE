"""
reranker — reordena resultados del RAG con un cross-encoder (Nivel 4+).

Two-stage retrieval: ChromaDB devuelve muchos candidatos por similitud de
vectores (rápido, aproximado); el cross-encoder reordena leyendo query+doc
JUNTOS (preciso), lo que mejora la relevancia — sobre todo con queries en
español sobre código.

Carga perezosa del modelo (pesado, ~GB): solo se carga la primera vez que se
rerankea, no al importar. Si sentence-transformers o el modelo no están
disponibles, degrada a "sin rerank" (orden original) — nunca crashea.
"""

from minka.config_loader import config

_model = None
_load_failed = False


def _get_model():
    """Carga perezosa del CrossEncoder. Devuelve None si no se puede cargar."""
    global _model, _load_failed
    if _model is not None:
        return _model
    if _load_failed:
        return None
    try:
        from sentence_transformers import CrossEncoder

        model_name = getattr(config, "RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
        _model = CrossEncoder(model_name)
        return _model
    except Exception:  # noqa: BLE001 — sin lib/modelo: degrada a sin-rerank
        _load_failed = True
        return None


def rerank(query: str, documents: list[str], top_n: int) -> list[tuple[int, float | None]]:
    """
    Reordena `documents` por relevancia real a `query` con el cross-encoder y
    devuelve [(idx_original, score), ...] de los top_n mejores (desc por score).

    Si el modelo no está disponible o falla, devuelve los primeros top_n en su
    orden original con score None (fallback) — nunca crashea.
    """
    if not documents:
        return []
    model = _get_model()
    if model is None:
        return [(i, None) for i in range(min(top_n, len(documents)))]
    try:
        scores = model.predict([(query, d) for d in documents])
    except Exception:  # noqa: BLE001 — cualquier fallo → orden original
        return [(i, None) for i in range(min(top_n, len(documents)))]
    order = sorted(range(len(documents)), key=lambda i: float(scores[i]), reverse=True)
    return [(i, round(float(scores[i]), 4)) for i in order[:top_n]]
