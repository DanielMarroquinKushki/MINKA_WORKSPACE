"""
config.example.py — Plantilla para el equipo.

Copia este archivo a `<DATA_DIR>/config_local.py` y completa TUS rutas de repos
(<DATA_DIR> = la variable DATA_DIR_ENV de _app.py o, si no está, ~/.<APP_NAME>/):
    cp config.example.py <DATA_DIR>/config_local.py

`config_local.py` está en .gitignore — no lo commitees.
Solo define las constantes que quieras sobreescribir; el resto se hereda
de config.py automáticamente vía config_loader.py.
"""

# Rutas ABSOLUTAS de los repos del proyecto en TU equipo: {alias: ruta}.
# Los alias deben coincidir con los del proyecto (p. ej. las claves de
# knowledge/extracted/_sources.json). No dejes una ruta vacía: "" es el cwd.
REPOS = {
    "mi-repo": "/ruta/absoluta/a/mi-repo",
}

# Pilares del equipo: normalmente van en projects/<proyecto>/config.py, no aquí.
# Vacíos = el gate y las lentes los omiten.
# TEAM_PILLARS = ["  01 Nombre del pilar — ¿pregunta de control?"]
# TEAM_PILLARS_HEADING = "Pilares del equipo"
# TEAM_PILLARS_READABILITY = ""
# TEAM_PILLARS_LINE_CAP_HINT = ""

# Opcional: nivel de autonomía. 3 = siempre pide aprobación (recomendado al inicio).
# AUTONOMY_LEVEL = 3
