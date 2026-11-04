"""baseline — huella (sha256) de la línea base de prompts + tools + modelos.

Permite saber, desde `minka stats`, si una comparación de costos o calidad se hizo
sobre la misma línea base: cualquier cambio en un system prompt (del proyecto activo o
del motor), en los schemas de tools que ve cada agente o en `config.MODELS` cambia el hash.
El knowledge NO entra: cambia con cada extracción y no es parte del prompt versionado.

Se calcula una vez por proceso y proyecto activo; nunca llama al modelo.
"""

from __future__ import annotations

import hashlib
import json

from minka.config_loader import config
from minka.core.prompt_loader import load_system_prompt
from minka.core.prompt_registry import PROMPT_NAMES
from minka.core.tools.registry import ToolRegistry

HASH_CHARS = 12
_cache: dict[str | None, str] = {}


def _prompt(name: str) -> str:
    try:
        return load_system_prompt(name)
    except Exception:  # noqa: BLE001 — un agente sin prompt no rompe la huella
        return ""


def compute_prompt_hash(cfg=None) -> str:
    """sha256 (primeros HASH_CHARS hex) de prompts + tools por agente + MODELS."""
    cfg = cfg or config
    parts: dict[str, object] = {
        "prompts": {name: _prompt(name) for name in PROMPT_NAMES},
        "tools": {name: ToolRegistry.get_tools_for_agent(name) for name in PROMPT_NAMES},
        "models": dict(sorted((getattr(cfg, "MODELS", {}) or {}).items())),
    }
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:HASH_CHARS]


def prompt_hash(cfg=None) -> str:
    """Huella cacheada por proyecto activo (se recalcula al cambiar de proyecto)."""
    cfg = cfg or config
    key = getattr(cfg, "ACTIVE_PROJECT", None)
    if key not in _cache:
        _cache[key] = compute_prompt_hash(cfg)
    return _cache[key]


def reset_cache() -> None:
    _cache.clear()
