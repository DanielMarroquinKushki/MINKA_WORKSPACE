"""
vector_memory — memoria semántica de largo plazo con mem0 (Nivel 4+).

A diferencia de session_memory (RAM, efímera) y episodic_memory (JSON, búsqueda
por subcadena), esta destila y recupera "recuerdos" por similitud SEMÁNTICA:
mem0 extrae hechos de las conversaciones (vía LLM) y los indexa en un vector
store (ChromaDB, el mismo que ya usa el RAG).

Carga perezosa + degradación segura: si mem0/lib/LLM no están disponibles o el
flag está apagado, todos los métodos no-opean (devuelven vacío/False) — nunca
crashea ni bloquea el flujo. Gate: config.VECTOR_MEMORY_ENABLED.

NOTA: la config de proveedor de mem0 (LLM + embedder Bedrock) y la extracción de
hechos requieren sesión LLM; se validan en la tanda SSO. Sin sesión, degrada.
"""

from config_loader import config


def _mem0_config() -> dict:
    """Config de mem0: vector store en ChromaDB + LLM/embedder por Bedrock.

    El LLM usa un Claude 3 (config.MEM0_LLM_MODEL) porque mem0 envía `temperature`
    y los Claude más nuevos la deprecaron (400). Validado contra Bedrock 2026-08-26.
    """
    return {
        "vector_store": {
            "provider": "chroma",
            "config": {
                "path": str(config.CHROMA_PATH),
                "collection_name": "agent_memory",
            },
        },
        "llm": {
            "provider": "aws_bedrock",
            "config": {
                "model": getattr(
                    config, "MEM0_LLM_MODEL", "us.anthropic.claude-3-haiku-20240307-v1:0"
                )
            },
        },
        "embedder": {
            "provider": "aws_bedrock",
            "config": {
                "model": getattr(config, "MEM0_EMBEDDER_MODEL", "amazon.titan-embed-text-v2:0")
            },
        },
    }


class VectorMemory:
    def __init__(self, namespace: str = "global"):
        self.namespace = namespace
        self._mem = None
        self._init_failed = False

    def _get_mem(self):
        """Lazy init de mem0. Devuelve None si está desactivado o no se puede."""
        if self._mem is not None:
            return self._mem
        if self._init_failed or not getattr(config, "VECTOR_MEMORY_ENABLED", False):
            return None
        try:
            from mem0 import Memory

            self._mem = Memory.from_config(_mem0_config())
            return self._mem
        except Exception:  # noqa: BLE001 — sin lib/config: degrada a no-op
            self._init_failed = True
            return None

    @staticmethod
    def _unwrap(res):
        """mem0 devuelve list o {'results': [...]} según versión — normaliza a list."""
        if isinstance(res, dict):
            return res.get("results", [])
        return res or []

    def add(self, text: str, metadata: dict | None = None) -> bool:
        """Guarda un recuerdo (mem0 extrae hechos vía LLM). False si no disponible."""
        mem = self._get_mem()
        if mem is None:
            return False
        try:
            mem.add(text, user_id=self.namespace, metadata=metadata or {})
            return True
        except Exception:  # noqa: BLE001
            return False

    def search(self, query: str, limit: int = 5) -> list:
        """Recupera recuerdos relevantes por similitud semántica. [] si no disponible."""
        mem = self._get_mem()
        if mem is None:
            return []
        try:
            return self._unwrap(
                mem.search(query, filters={"user_id": self.namespace}, limit=limit)
            )
        except Exception:  # noqa: BLE001
            return []

    def get_all(self) -> list:
        """Devuelve todos los recuerdos del namespace. [] si no disponible."""
        mem = self._get_mem()
        if mem is None:
            return []
        try:
            return self._unwrap(mem.get_all(filters={"user_id": self.namespace}))
        except Exception:  # noqa: BLE001
            return []
