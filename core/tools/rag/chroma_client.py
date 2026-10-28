"""
chroma_client — cliente ChromaDB (Nivel 4, stub).

Activar: pip install chromadb  +  config.RAG_ENABLED = True
"""

from config_loader import config


def get_client():
    """Devuelve un PersistentClient de ChromaDB. Requiere RAG_ENABLED."""
    if not config.RAG_ENABLED:
        raise NotImplementedError(
            "RAG desactivado. Pon RAG_ENABLED=True e instala chromadb (Nivel 4)."
        )
    import chromadb  # import perezoso: solo si RAG está activo

    return chromadb.PersistentClient(path=str(config.CHROMA_PATH))
