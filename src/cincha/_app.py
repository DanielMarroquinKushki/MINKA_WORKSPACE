"""_app.py — identidad de la app: único lugar para el nombre (provisional).

Sin efectos al importar: config.py y tests/conftest.py lo usan antes de cargar
la configuración (los tests fijan el directorio de datos ANTES de importar config).
"""

APP_NAME = "cincha"
DATA_DIR_ENV = f"{APP_NAME.upper()}_HOME"
