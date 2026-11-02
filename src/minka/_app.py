"""_app.py — identidad de la app: único lugar para el nombre (provisional).

Sin efectos al importar: config.py y tests/conftest.py lo usan antes de cargar
la configuración (los tests fijan el directorio de datos ANTES de importar config).
"""

APP_NAME = "minka"
DATA_DIR_ENV = f"{APP_NAME.upper()}_HOME"
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
