"""
dependency_mapper — mapea dependencias y detecta version drift entre repos.
"""

from cincha.config_loader import config
from cincha.core.tools.analysis.go_analyzer import check_core_version


def map_dependencies(path: str) -> str:
    """
    Extrae los require directos del go.mod (dependencias externas del servicio).
    """
    from pathlib import Path

    gomod = Path(path) / "go.mod"
    if not gomod.is_file():
        return f"ERROR: no hay go.mod en {path}"

    lines = gomod.read_text(encoding="utf-8", errors="replace").splitlines()
    deps: list[str] = []
    in_require = False
    for line in lines:
        s = line.strip()
        if s.startswith("require ("):
            in_require = True
            continue
        if in_require and s == ")":
            in_require = False
            continue
        if (in_require or s.startswith("require ")) and "/" in s:
            deps.append(s.replace("require ", "").strip())
    return "\n".join(deps) or "[sin dependencias externas]"


def check_version_drift(repos: dict) -> str:
    """
    Compara la versión del módulo core (config.CORE_MODULE_NAME) y de Go
    entre los repos configurados.
    `repos` es el dict config.REPOS {alias: ruta}.

    Devuelve un reporte que marca si hay divergencia — riesgo real detectado.
    """
    core_module = getattr(config, "CORE_MODULE_NAME", "")
    rows: list[str] = []
    for alias, path in repos.items():
        if not path:
            rows.append(f"{alias}: [ruta no configurada]")
            continue
        rows.append(f"{alias}: {check_core_version(path)}")

    header = "Versiones por repo:\n" + "\n".join(rows)
    if not core_module:
        return header + "\n\n[CORE_MODULE_NAME no configurado: no se compara drift del core.]"

    # Detectar drift de core
    core_versions = set()
    for row in rows:
        if f"{core_module}=" in row:
            core_versions.add(row.split(f"{core_module}=")[1].strip())
    drift = (
        f"\n\n⚠️ VERSION DRIFT: los repos usan versiones distintas de {core_module}."
        if len(core_versions) > 1
        else f"\n\nOK: versiones de {core_module} consistentes."
    )
    return header + drift
