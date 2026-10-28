"""
loader.py — Carga la base de conocimiento (.md) y la inyecta a los agentes.

Fix del bug del prompt original: `KnowledgeLoader.load_for_agent(name)` se
llamaba sin existir. Aquí es real.

Diseño:
- La knowledge base son archivos .md en knowledge/{extracted,professional,team}/.
- Cada agente recibe un subconjunto relevante a su rol (no todo, para no gastar
  contexto ni ruido).
- `KnowledgeBundle.get_relevant_context()` concatena esos .md en un bloque de
  texto listo para el system prompt.

Nivel 4 (RAG): cuando RAG_ENABLED, este loader podrá delegar la selección de
contexto a búsqueda semántica en vez de cargar archivos completos. Hoy: carga
por rol (determinista y barato).
"""

from pathlib import Path

from config_loader import config

# Qué archivos .md ve cada agente: vive en config.AGENT_KNOWLEDGE
# (parametrizable por proyecto — Arnés Global).


class KnowledgeBundle:
    """Conjunto de documentos de conocimiento cargados para un agente."""

    def __init__(self, agent_name: str, docs: dict[str, str]):
        self.agent_name = agent_name
        self.docs = docs  # {ruta_relativa: contenido}

    def get_relevant_context(self) -> str:
        """Concatena los docs cargados en un bloque para el system prompt."""
        if not self.docs:
            return "[Base de conocimiento vacía — ejecuta la extracción del knowledge base.]"
        parts = ["# BASE DE CONOCIMIENTO (dev-agent)\n"]
        for rel, content in self.docs.items():
            parts.append(f"\n## Documento: {rel}\n\n{content}\n")
        return "\n".join(parts)

    def __len__(self) -> int:
        return len(self.docs)


class KnowledgeLoader:
    @staticmethod
    def load_for_agent(agent_name: str) -> KnowledgeBundle:
        """Carga los .md relevantes al rol del agente. Busca primero en el
        knowledge del proyecto activo; si el doc no está ahí, cae al
        knowledge/ del motor (docs genéricos)."""
        bases: list[Path] = []
        project = getattr(config, "ACTIVE_PROJECT", None)
        if project:
            bases.append(config.PROJECTS_DIR / project / "knowledge")
        bases.append(config.KNOWLEDGE_DIR)

        wanted = getattr(config, "AGENT_KNOWLEDGE", {}).get(agent_name, [])
        docs: dict[str, str] = {}
        for rel in wanted:
            for base in bases:
                f = base / rel
                if f.is_file():
                    docs[rel] = f.read_text(encoding="utf-8", errors="replace")
                    break
            # Si falta en todas las bases, se omite silenciosamente:
            # la KB se genera por fases.
        return KnowledgeBundle(agent_name, docs)
