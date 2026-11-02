"""small_talk — respuesta fija a saludos/agradecimientos, sin llamar al LLM.

Determinista: el pedido actual (sin el historial que antepone main.py) se
normaliza y debe coincidir COMPLETO con uno de config.SMALL_TALK_PATTERNS y no
pasar de config.SMALL_TALK_MAX_WORDS palabras. "hola, analiza X" no coincide.
"""

import re
import unicodedata

from minka.config_loader import config

# main.py separa el historial del pedido actual con este marcador.
CURRENT_REQUEST_MARKER = "Pedido actual del dev:"


def _normalize(text: str) -> str:
    text = text.rsplit(CURRENT_REQUEST_MARKER, 1)[-1]
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def small_talk_reply(task: str, agent_names: tuple[str, ...]) -> str | None:
    """Texto fijo si `task` es solo un saludo/agradecimiento; None si no."""
    text = _normalize(task)
    if not text or len(text.split()) > getattr(config, "SMALL_TALK_MAX_WORDS", 4):
        return None
    patterns = getattr(config, "SMALL_TALK_PATTERNS", [])
    if not any(re.fullmatch(p, text) for p in patterns):
        return None
    agents = ", ".join(n.removesuffix("_agent") for n in agent_names)
    return (
        f"¡Hola! Soy {config.APP_NAME}. Pídeme una tarea concreta (analizar, revisar, "
        f"depurar, documentar o desarrollar). Agentes disponibles: {agents}."
    )
