"""DATA_DIR: variable de entorno o ~/.<APP_NAME>; de él derivan las rutas de datos."""

import importlib.util
from pathlib import Path

from cincha import config as config_py


def _guard_env(monkeypatch, *keys: str) -> None:
    """Quita las claves y deja que monkeypatch las restaure aunque otro (setdefault,
    load_dotenv) las escriba directo en os.environ."""
    for key in keys:
        monkeypatch.setenv(key, "")
        monkeypatch.delenv(key)


def _fresh_config(monkeypatch, home: Path | None, env_keys: tuple = ()):
    """Ejecuta config.py como módulo nuevo (no recarga el config global)."""
    if home is None:
        monkeypatch.delenv(config_py.DATA_DIR_ENV, raising=False)
    else:
        monkeypatch.setenv(config_py.DATA_DIR_ENV, str(home))
    # config.py hace setdefault de DEEPEVAL_CACHE_FOLDER y carga el .env de DATA_DIR.
    _guard_env(monkeypatch, "DEEPEVAL_CACHE_FOLDER", *env_keys)
    spec = importlib.util.spec_from_file_location("_config_fresh", config_py.__file__)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_data_dir_desde_variable(tmp_path, monkeypatch):
    mod = _fresh_config(monkeypatch, tmp_path)
    assert mod.DATA_DIR == tmp_path
    assert mod.PROJECTS_DIR == tmp_path / "projects"
    assert mod.OUTPUT_DIR == tmp_path / "output"
    assert mod.CHROMA_PATH == tmp_path / ".chromadb"
    assert mod.TEMPLATE_DIR == Path(config_py.__file__).parent / "projects" / "_template"


def test_data_dir_default_en_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    mod = _fresh_config(monkeypatch, None)
    assert mod.DATA_DIR == tmp_path / ".cincha"
    assert mod.PROJECTS_DIR == tmp_path / ".cincha" / "projects"


def test_env_desde_data_dir(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LINE_CAP=77\n", encoding="utf-8")
    mod = _fresh_config(monkeypatch, tmp_path, env_keys=("LINE_CAP",))
    assert mod.LINE_CAP == 77
