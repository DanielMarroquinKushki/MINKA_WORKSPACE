"""
project_scaffold — crea el esqueleto de un proyecto nuevo (Arnés Global).

Lógica pura y testeable: create_project() no imprime ni pregunta nada; solo
copia el _template, renombra los .example, crea el active-context y devuelve un
resumen. La capa interactiva (preguntas/prints) vive en main.py (--new-project).

Datos en config.DATA_DIR (proyectos y active-context); plantilla en
config.TEMPLATE_DIR — sin rutas hardcodeadas.
"""

import re
import shutil
from pathlib import Path

from minka.config_loader import config

# Nombre válido: kebab-case (minúsculas, números, guiones internos).
_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# .example → nombre real (se renombran tras copiar el template).
_EXAMPLE_RENAMES = {
    "config.py.example": "config.py",
    "knowledge/team/session_log.md.example": "knowledge/team/session_log.md",
    "knowledge/team/decisions_log.md.example": "knowledge/team/decisions_log.md",
}

# Plantilla del contexto de corto plazo (fuente única en active-context/).
_ACTIVE_CONTEXT_TEMPLATE = """\
# Contexto Activo — {title}
*Proyecto: {name}*

## Ticket actual
(por definir)

## Alcance
(qué se va a tocar en este ticket)

## Bloqueos
Ninguno activo

## Próximo paso
(la siguiente acción concreta)

## Memoria del proyecto
- Historial de sesiones → `dev-agent/projects/{name}/knowledge/team/session_log.md`
- Decisiones (ADRs) → `dev-agent/projects/{name}/knowledge/team/decisions_log.md`
- Arquitectura / flujos → `dev-agent/projects/{name}/knowledge/extracted/`
"""


class ProjectExistsError(Exception):
    """El proyecto ya existe — no se sobreescribe."""


class InvalidProjectNameError(Exception):
    """El nombre no cumple kebab-case o está reservado."""


def _title(name: str) -> str:
    return name.replace("-", " ").title()


# Plantillas de datos del usuario que viajan en el paquete → destino en DATA_DIR.
_INIT_FILES = {
    "config.example.py": "config_local.py",
    ".env.example": ".env",
}


def init_data_dir() -> dict:
    """Copia config.example.py y .env.example a config.DATA_DIR (config_local.py, .env).
    Nunca sobrescribe. Devuelve {"data_dir", "created": [...], "skipped": [...]}."""
    data_dir = Path(config.DATA_DIR)
    data_dir.mkdir(parents=True, exist_ok=True)
    created, skipped = [], []
    for src_name, dst_name in _INIT_FILES.items():
        dst = data_dir / dst_name
        if dst.exists():
            skipped.append(str(dst))
            continue
        shutil.copyfile(Path(config.BASE_DIR) / src_name, dst)
        created.append(str(dst))
    return {"data_dir": str(data_dir), "created": created, "skipped": skipped}


def apply_init_answers(
    data_dir: Path,
    repos: dict[str, str],
    backend: str,
    aws_profile: str,
    created: list[str],
) -> list[str]:
    """Escribe las respuestas de `minka init` SOLO en los archivos que se acaban de crear
    (nunca pisa un config_local.py o .env previos). Devuelve los archivos tocados."""
    touched: list[str] = []
    local = data_dir / "config_local.py"
    if repos and str(local) in created:
        text = local.read_text(encoding="utf-8")
        block = "REPOS = {\n" + "".join(f'    {alias!r}: {path!r},\n' for alias, path in repos.items()) + "}\n"
        new_text, n = re.subn(r"REPOS = \{.*?\n\}\n", block, text, count=1, flags=re.DOTALL)
        local.write_text(new_text if n else text + "\n" + block, encoding="utf-8")
        touched.append(str(local))
    env = data_dir / ".env"
    if str(env) in created:
        lines = env.read_text(encoding="utf-8").splitlines()
        values = {"LLM_BACKEND": backend, "AWS_PROFILE": aws_profile}
        seen = set()
        for i, line in enumerate(lines):
            key = line.split("=", 1)[0].strip()
            if key in values and not line.lstrip().startswith("#"):
                lines[i] = f"{key}={values[key]}"
                seen.add(key)
        lines += [f"{k}={v}" for k, v in values.items() if k not in seen]
        env.write_text("\n".join(lines) + "\n", encoding="utf-8")
        touched.append(str(env))
    return touched


STACKS = ("go", "python", "node")
_STACK_MARKERS = (
    ("go.mod", "go"),
    ("pyproject.toml", "python"),
    ("setup.py", "python"),
    ("requirements.txt", "python"),
    ("package.json", "node"),
)


def detect_stack(repo_path: str | Path) -> str | None:
    """Stack según los archivos del repo (go.mod → go, pyproject/setup/requirements → python,
    package.json → node). None si no hay marcador."""
    root = Path(repo_path).expanduser()
    for marker, stack in _STACK_MARKERS:
        if (root / marker).is_file():
            return stack
    return None


def plan_repos(repos: dict[str, str], repos_root: str | None) -> tuple[dict[str, str], str | None]:
    """Decide cómo escribir REPOS en el proyecto.

    Devuelve (repos_para_config, repos_root_a_fijar):
    - Si todos los repos comparten carpeta y config_local no tiene REPOS_ROOT → rutas
      relativas y esa carpeta como REPOS_ROOT a escribir en config_local.
    - Si comparten carpeta y coincide con el REPOS_ROOT ya configurado → relativas, nada que fijar.
    - En cualquier otro caso → rutas absolutas, nada que fijar.
    """
    absolute = {alias: str(Path(p).expanduser().resolve()) for alias, p in repos.items()}
    parents = {str(Path(p).parent) for p in absolute.values()}
    if len(parents) != 1:
        return absolute, None
    common = parents.pop()
    configured = None
    if repos_root and Path(repos_root).expanduser().is_dir():
        configured = str(Path(repos_root).expanduser().resolve())
    # Sin REPOS_ROOT, o con el placeholder de la plantilla (carpeta inexistente): se fija aquí.
    if configured is None:
        return {a: Path(p).name for a, p in absolute.items()}, common
    if configured == common:
        return {a: Path(p).name for a, p in absolute.items()}, None
    return absolute, None


def write_project_settings(
    config_path: Path,
    stack: str | None = None,
    compliance: list[str] | None = None,
    repos: dict[str, str] | None = None,
    rag: bool | None = None,
) -> None:
    """Escribe STACK, COMPLIANCE, REPOS y RAG_ENABLED en el config.py recién creado."""
    text = config_path.read_text(encoding="utf-8")
    if rag is not None:
        line = f"RAG_ENABLED = {rag}"
        text, n = re.subn(r"^# ?RAG_ENABLED = .*$|^RAG_ENABLED = .*$", line, text, count=1, flags=re.M)
        if not n:
            text += (
                "\n# Búsqueda semántica del código (Nivel 4): requiere el extra [rag] y "
                "`minka --index-repos`.\n" + line + "\n"
            )
    if stack:
        text, n = re.subn(r'^# ?STACK = .*$', f'STACK = "{stack}"', text, count=1, flags=re.M)
        if not n:
            text += f'\nSTACK = "{stack}"\n'
    if compliance is not None:
        value = "[" + ", ".join(f'"{c}"' for c in compliance) + "]"
        text, n = re.subn(r'^# ?COMPLIANCE = .*$', f"COMPLIANCE = {value}", text, count=1, flags=re.M)
        if not n:
            text += f"\nCOMPLIANCE = {value}\n"
    if repos is not None:
        block = "REPOS = {\n" + "".join(f"    {a!r}: {p!r},\n" for a, p in repos.items()) + "}"
        text, n = re.subn(r"^REPOS = \{.*?^\}", block, text, count=1, flags=re.M | re.S)
        if not n:
            text += f"\n{block}\n"
    config_path.write_text(text, encoding="utf-8")


def ensure_repos_root(data_dir: Path, repos_root: str) -> bool:
    """Añade REPOS_ROOT a <data_dir>/config_local.py si no lo define. True si lo escribió."""
    local = data_dir / "config_local.py"
    text = local.read_text(encoding="utf-8") if local.is_file() else ""
    current = re.search(r'^REPOS_ROOT\s*=\s*["\'](.*)["\']\s*$', text, re.M)
    if current:
        if Path(current.group(1)).expanduser().is_dir():
            return False  # ya hay una raíz real: no se toca
        # Placeholder de la plantilla (carpeta inexistente): se reemplaza en su línea.
        text = text[: current.start()] + f"REPOS_ROOT = {repos_root!r}" + text[current.end() :]
        local.write_text(text, encoding="utf-8")
        return True
    if re.search(r"^REPOS_ROOT\s*=", text, re.M):
        return False
    data_dir.mkdir(parents=True, exist_ok=True)
    sep = "" if (not text or text.endswith("\n")) else "\n"
    local.write_text(text + f'{sep}\n# Carpeta donde viven los repos del proyecto (rutas relativas de REPOS).\nREPOS_ROOT = {repos_root!r}\n', encoding="utf-8")
    return True


def create_project(
    name: str,
    core_module: str | None = None,
    stack: str | None = None,
    compliance: list[str] | None = None,
    repos: dict[str, str] | None = None,
    rag: bool | None = None,
) -> dict:
    """
    Crea el esqueleto de un proyecto nuevo desde projects/_template.
    Devuelve dict con name, project_dir, active_context, next_steps, stack, repos y
    repos_root (carpeta que quedó fijada en config_local, o None).
    No imprime ni pregunta nada (capa pura, testeable).
    """
    if stack is not None and stack not in STACKS:
        raise InvalidProjectNameError(f"STACK inválido: '{stack}'. Opciones: {', '.join(STACKS)}.")
    if not name or not _NAME_RE.match(name):
        raise InvalidProjectNameError(
            f"Nombre inválido: '{name}'. Usa kebab-case (ej: mi-proyecto)."
        )
    if name == "_template":
        raise InvalidProjectNameError("'_template' es un nombre reservado.")

    projects_dir = Path(config.PROJECTS_DIR)
    template_dir = Path(config.TEMPLATE_DIR)
    project_dir = projects_dir / name

    if project_dir.exists():
        raise ProjectExistsError(f"El proyecto '{name}' ya existe: {project_dir}")
    if not template_dir.is_dir():
        raise FileNotFoundError(f"No existe el template: {template_dir}")

    # 1. Copia el template completo.
    shutil.copytree(template_dir, project_dir)

    # 2. Renombra los .example a sus nombres reales.
    for example_rel, real_rel in _EXAMPLE_RENAMES.items():
        example_path = project_dir / example_rel
        if example_path.is_file():
            example_path.rename(project_dir / real_rel)

    # 3. Crea el active-context (fuente única de corto plazo) en ACTIVE_CONTEXT_DIR
    #    (default <DATA_DIR>/active-context; config_local puede apuntar a otra carpeta).
    from minka.core.memory.live_context import active_context_dir as _ac_dir

    active_context_dir = _ac_dir(config)
    active_context_dir.mkdir(parents=True, exist_ok=True)
    active_context = active_context_dir / f"{name}.md"
    if not active_context.exists():
        active_context.write_text(
            _ACTIVE_CONTEXT_TEMPLATE.format(name=name, title=_title(name)),
            encoding="utf-8",
        )

    # 4. Escribe CORE_MODULE_NAME si se proporcionó.
    if core_module:
        _set_core_module(project_dir / "config.py", core_module)

    # 5. STACK, COMPLIANCE y REPOS (relativas a REPOS_ROOT si todos comparten carpeta).
    repos_for_config: dict[str, str] | None = None
    root_set: str | None = None
    if repos:
        repos_for_config, root_to_set = plan_repos(repos, getattr(config, "REPOS_ROOT", None))
        if root_to_set and ensure_repos_root(Path(config.DATA_DIR), root_to_set):
            root_set = root_to_set
    if stack or compliance is not None or repos_for_config is not None or rag is not None:
        write_project_settings(project_dir / "config.py", stack, compliance, repos_for_config, rag)

    steps = [
        f"Completa los parámetros en {project_dir / 'config.py'} "
        "(CORE_MODULE_NAME, PR_CHECKLIST, AGENT_KNOWLEDGE, AGENT_TOOLS)."
    ]
    if not repos:
        steps.append(
            f"Declara los repos en {project_dir / 'config.py'} → REPOS (rutas relativas) y "
            f"REPOS_ROOT en {Path(config.DATA_DIR) / 'config_local.py'}."
        )
    if rag is False:
        steps.append(
            f"RAG desactivado: para indexar, pon RAG_ENABLED = True en {project_dir / 'config.py'} "
            f"y corre {config.APP_NAME} --index-repos --project {name}."
        )
    else:
        steps.append(f"Indexa el código: {config.APP_NAME} --index-repos --project {name}")
    steps.append(f"Arranca: {config.APP_NAME} --project {name}")
    return {
        "name": name,
        "project_dir": str(project_dir),
        "active_context": str(active_context),
        "stack": stack,
        "compliance": compliance,
        "repos": repos_for_config or {},
        "repos_root": root_set,
        "next_steps": steps,
    }


def _set_core_module(config_path: Path, core_module: str) -> bool:
    """Reemplaza el valor de CORE_MODULE_NAME en el config.py del proyecto."""
    if not config_path.is_file():
        return False
    text = config_path.read_text(encoding="utf-8")
    new_text = re.sub(
        r'CORE_MODULE_NAME\s*=\s*["\'].*?["\']',
        f'CORE_MODULE_NAME = "{core_module}"',
        text,
        count=1,
    )
    if new_text != text:
        config_path.write_text(new_text, encoding="utf-8")
        return True
    return False
