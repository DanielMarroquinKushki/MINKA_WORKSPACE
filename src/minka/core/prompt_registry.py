"""
prompt_registry — versionado/despliegue de prompts con Langfuse Prompts (Ola 3).

Permite sacar los system prompts de los archivos *_prompt.py a un registro
versionado (Langfuse): cada cambio queda como versión, con rollback y labels.

Guard: config.PROMPT_REGISTRY_ENABLED + credenciales Langfuse en el entorno.
Si está off o el servicio no está disponible, get_prompt devuelve None y
load_system_prompt cae a los archivos (comportamiento actual) — nunca crashea.
Requiere el servicio Langfuse para operar; se valida cuando esté arriba.
"""

from minka.config_loader import config

# Prompts que se versionan (6 agentes + orchestrator).
PROMPT_NAMES = [
    "architecture_agent",
    "development_agent",
    "pr_review_agent",
    "debug_agent",
    "report_agent",
    "meta_agent",
    "orchestrator",
]

_client = None
_init_failed = False


def _get_client():
    """Lazy init del cliente Langfuse. None si desactivado o no configurable."""
    global _client, _init_failed
    if _client is not None:
        return _client
    if _init_failed or not getattr(config, "PROMPT_REGISTRY_ENABLED", False):
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse()  # lee LANGFUSE_* del entorno
        return _client
    except Exception:  # noqa: BLE001 — sin lib/keys: no-op
        _init_failed = True
        return None


def get_prompt(name: str) -> str | None:
    """Trae el prompt versionado `name` de Langfuse. None si no disponible."""
    client = _get_client()
    if client is None:
        return None
    try:
        prompt = client.get_prompt(name)
        text = getattr(prompt, "prompt", None)
        return text if isinstance(text, str) and text.strip() else None
    except Exception:  # noqa: BLE001 — no versionado / servicio caído → None (fallback a archivo)
        return None


def push_prompt(name: str, text: str, label: str = "production") -> bool:
    """Sube/actualiza el prompt `name` en Langfuse (nueva versión). False si no disponible."""
    client = _get_client()
    if client is None:
        return False
    try:
        client.create_prompt(name=name, prompt=text, labels=[label], type="text")
        return True
    except Exception:  # noqa: BLE001
        return False


def seed_registry() -> dict:
    """Sube los prompts actuales (del proyecto activo) al registro. Conteos."""
    from minka.core.prompt_loader import load_system_prompt

    pushed, skipped = [], []
    for name in PROMPT_NAMES:
        try:
            text = load_system_prompt(name)
        except Exception:  # noqa: BLE001 — el proyecto no define ese prompt
            skipped.append(name)
            continue
        (pushed if push_prompt(name, text) else skipped).append(name)
    return {"pushed": pushed, "skipped": skipped}
