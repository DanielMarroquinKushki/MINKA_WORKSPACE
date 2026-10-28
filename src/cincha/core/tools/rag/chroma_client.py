"""
chroma_client — cliente ChromaDB (Nivel 4, stub).

Activar: instala el extra rag (cincha[rag])  +  config.RAG_ENABLED = True
"""

from cincha.config_loader import config


def get_client():
    """Devuelve un PersistentClient de ChromaDB. Requiere RAG_ENABLED."""
    if not config.RAG_ENABLED:
        raise NotImplementedError(
            "RAG desactivado. Pon RAG_ENABLED=True e instala chromadb (Nivel 4)."
        )
    try:
        import chromadb  # import perezoso: solo si RAG está activo
    except ImportError as exc:
        raise RuntimeError(
            "RAG_ENABLED=True pero chromadb no está instalado. Instala el extra rag: "
            "uv tool install 'cincha[rag]'."
        ) from exc

    return chromadb.PersistentClient(path=str(config.CHROMA_PATH))
