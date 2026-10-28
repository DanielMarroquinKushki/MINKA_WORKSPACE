"""
prompt_loader.py — carga el system prompt de cada agente según el proyecto.

Busca projects/<ACTIVE_PROJECT>/prompts/<nombre>_prompt.py y extrae la
constante *_SYSTEM_PROMPT. Si el proyecto no define el prompt, cae al
paquete prompts/ del repo (defaults del motor).

Convención de nombres (cubre los 6 agentes + orchestrator):
    "architecture_agent" → architecture_prompt.py / ARCHITECTURE_SYSTEM_PROMPT
    "orchestrator"       → orchestrator_prompt.py / ORCHESTRATOR_SYSTEM_PROMPT
"""

import importlib

from cincha.config_loader import config, exec_file_module


def load_system_prompt(agent_name: str) -> str:
    stem = agent_name.removesuffix("_agent")
    module_name = f"{stem}_prompt"
    var_name = f"{stem.upper()}_SYSTEM_PROMPT"

    # 1. Registro versionado (Langfuse Prompts) si está activo. Fallback seguro:
    #    si no está o el servicio no responde, get_prompt devuelve None.
    if getattr(config, "PROMPT_REGISTRY_ENABLED", False):
        from cincha.core.prompt_registry import get_prompt

        versioned = get_prompt(agent_name)
        if versioned:
            return versioned

    project = getattr(config, "ACTIVE_PROJECT", None)
    if project:
        f = config.PROJECTS_DIR / project / "prompts" / f"{module_name}.py"
        if f.is_file():
            return getattr(exec_file_module(f"_prj_{module_name}", f), var_name)

    # Fallback: prompts/ del repo (motor).
    return getattr(importlib.import_module(f"cincha.core.prompts.{module_name}"), var_name)
