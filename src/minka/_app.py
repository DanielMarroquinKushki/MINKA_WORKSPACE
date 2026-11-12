"""_app.py — identidad de la app: único lugar para el nombre (provisional).

Sin efectos al importar: config.py y tests/conftest.py lo usan antes de cargar
la configuración (los tests fijan el directorio de datos ANTES de importar config).
"""

APP_NAME = "minka"
DATA_DIR_ENV = f"{APP_NAME.upper()}_HOME"
# Quién lanzó la corrida (repl|cli|bridge|evals) cuando no es la propia CLI; y
# apagado de la línea final de métricas sin cambiar el comando (p.ej. desde el bridge).
SOURCE_ENV = f"{APP_NAME.upper()}_SOURCE"
QUIET_ENV = f"{APP_NAME.upper()}_QUIET"
# Salida sin renderizar Markdown (equivale a --plain); ver core/render.py.
PLAIN_ENV = f"{APP_NAME.upper()}_PLAIN"
DIST_NAME = f"{APP_NAME}-workspace"  # nombre de la distribución (pip / uv)
_INSTALL_PLACEHOLDER = "git+<url-del-repo>"


def _direct_url() -> dict | None:
    """direct_url.json (PEP 610) del paquete instalado, o None."""
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        raw = distribution(DIST_NAME).read_text("direct_url.json")
    except PackageNotFoundError:
        return None
    return json.loads(raw) if raw else None


def version_text() -> str:
    """`minka --version`: versión instalada, commit y revisión pedida (direct_url.json, PEP 610).
    Sin paquete instalado o sin direct_url: lo dice (instalación editable o desde wheel local)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        ver = version(DIST_NAME)
    except PackageNotFoundError:
        return f"{APP_NAME} (sin paquete instalado: ejecución desde el código fuente)"
    info = _direct_url() or {}
    vcs = info.get("vcs_info") or {}
    if vcs:
        commit = str(vcs.get("commit_id", ""))[:12] or "?"
        rev = vcs.get("requested_revision") or "sin revisión pedida (rama por defecto)"
        return f"{APP_NAME} {ver} · commit {commit} · requested_revision: {rev}"
    if info.get("url"):
        return f"{APP_NAME} {ver} · instalado desde {info['url']} (sin control de versiones)"
    return f"{APP_NAME} {ver} · instalación editable o local: sin direct_url.json"


def install_source(configured: str = "") -> str:
    """Origen para `uv tool install`: direct_url.json > config_local > texto de ejemplo."""
    info = _direct_url()
    if info and info.get("url"):
        vcs = info.get("vcs_info")
        if vcs:
            rev = vcs.get("requested_revision")
            return f"{vcs['vcs']}+{info['url']}" + (f"@{rev}" if rev else "")
        return info["url"]
    return configured or _INSTALL_PLACEHOLDER
