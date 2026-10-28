"""
searcher.py — Búsqueda semántica sobre el codebase indexado (Nivel 4).
chromadb se importa perezoso vía chroma_client/indexer (solo con RAG activo).

Two-stage retrieval opcional: si config.RERANK_ENABLED, se traen más candidatos
de ChromaDB (RERANK_CANDIDATES) y se reordenan con un cross-encoder local
(core.tools.rag.reranker) antes de devolver los n_results mejores. El reranker
degrada a "sin rerank" si el modelo no está disponible.
"""

from minka.core.tools.rag.chroma_client import get_client
from minka.core.tools.rag.indexer import get_or_create_collection


def semantic_search(
    query: str,
    config,
    n_results: int = 10,
    repo_filter: str = None,
    collection: str = "codebase",
) -> list[dict]:
    """
    Búsqueda semántica sobre una colección indexada.

    Args:
        query: pregunta en lenguaje natural
        n_results: número de resultados a devolver
        repo_filter: filtrar por repo ("pos", "hsm", "core") — solo aplica a "codebase"
        collection: "codebase" (código Go) o "knowledge_docs" (.md)

    Returns:
        Lista de resultados con file, name/section, type, start_line, repo/source,
        relevance (distancia vectorial) y snippet. Si hay reranking, además
        rerank_score y el orden refleja el reranker.
    """
    if not config.RAG_ENABLED:
        return [{"error": "RAG_ENABLED=False — activa el RAG primero"}]

    try:
        client = get_client()
        coll = get_or_create_collection(client, collection)

        if coll.count() == 0:
            hint = "--index-knowledge" if collection == "knowledge_docs" else "--index-repos"
            return [{"error": f"Índice '{collection}' vacío — ejecuta {hint} primero"}]

        where = {"repo": repo_filter} if repo_filter else None

        # Two-stage: si hay rerank, traemos más candidatos para reordenarlos.
        rerank_on = getattr(config, "RERANK_ENABLED", False)
        fetch_n = getattr(config, "RERANK_CANDIDATES", 50) if rerank_on else n_results

        results = coll.query(
            query_texts=[query],
            n_results=fetch_n,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        docs = results["documents"][0]
        metas = results["metadatas"][0]
        dists = results["distances"][0]

        # Orden final: reranker [(idx, score)] o el orden vectorial original.
        if rerank_on:
            from minka.core.tools.rag.reranker import rerank

            ordered = rerank(query, docs, n_results)
        else:
            ordered = [(i, None) for i in range(min(n_results, len(docs)))]

        output = []
        for out_rank, (idx, rerank_score) in enumerate(ordered, 1):
            item = {
                "rank": out_rank,
                "repo": metas[idx].get("repo"),
                "source": metas[idx].get("source"),
                "file": metas[idx].get("file"),
                "name": metas[idx].get("name") or metas[idx].get("section"),
                "type": metas[idx].get("type"),
                "start_line": metas[idx].get("start_line"),
                "relevance": round(1 - dists[idx], 3),
                "snippet": docs[idx][:500],
            }
            if rerank_score is not None:
                item["rerank_score"] = rerank_score
            output.append(item)

        return output

    except Exception as e:
        return [{"error": str(e)}]
