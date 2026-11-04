"""
config.example.py — Plantilla para el equipo.

Copia este archivo a `<DATA_DIR>/config_local.py` y completa TUS rutas de repos
(<DATA_DIR> = la variable DATA_DIR_ENV de _app.py o, si no está, ~/.<APP_NAME>/):
    cp config.example.py <DATA_DIR>/config_local.py

`config_local.py` está en .gitignore — no lo commitees.
Solo define las constantes que quieras sobreescribir; el resto se hereda
de config.py automáticamente vía config_loader.py.
"""

# Repos: los alias y sus rutas RELATIVAS viven en projects/<proyecto>/config.py → REPOS.
# Aquí solo va la carpeta de TU equipo donde están clonados (REPOS_ROOT) y, si algún repo
# vive en otro lado, su ruta puntual en REPOS_OVERRIDES.
REPOS_ROOT = "~/ruta/donde/clonas/los/repos"
# REPOS_OVERRIDES = {"mi-repo": "/otra/ruta/absoluta/a/mi-repo"}

# Esquema viejo (sigue funcionando, `minka doctor` lo marca como deprecado): REPOS con
# rutas absolutas aquí, solo si el proyecto no define REPOS.
# REPOS = {"mi-repo": "/ruta/absoluta/a/mi-repo"}

# Pilares del equipo: normalmente van en projects/<proyecto>/config.py, no aquí.
# Vacíos = el gate y las lentes los omiten.
# TEAM_PILLARS = ["  01 Nombre del pilar — ¿pregunta de control?"]
# TEAM_PILLARS_HEADING = "Pilares del equipo"
# TEAM_PILLARS_READABILITY = ""
# TEAM_PILLARS_LINE_CAP_HINT = ""

# Opcional: nivel de autonomía. 3 = siempre pide aprobación (recomendado al inicio).
# AUTONOMY_LEVEL = 3

# Opcional: carpeta con el contexto vivo por proyecto (<proyecto>.md). Por defecto
# <DATA_DIR>/active-context; descoméntalo si vive en otra parte (p. ej. la raíz del workspace).
# ACTIVE_CONTEXT_DIR = "~/ruta/al/workspace/active-context"
