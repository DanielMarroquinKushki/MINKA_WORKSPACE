"""config_local.py se carga por ruta desde DATA_DIR (sin sys.path ni sys.modules)."""

import sys

from cincha import config_loader


def test_config_local_desde_data_dir(tmp_path, monkeypatch):
    assert config_loader._load_local_vars(tmp_path) == {}

    (tmp_path / "config_local.py").write_text(
        'AUTONOMY_LEVEL = 4\nREPOS = {"demo": "/ruta/demo"}\n', encoding="utf-8"
    )
    path_antes = list(sys.path)
    assert config_loader._load_local_vars(tmp_path) == {
        "AUTONOMY_LEVEL": 4,
        "REPOS": {"demo": "/ruta/demo"},
    }
    monkeypatch.setattr(config_loader._base, "DATA_DIR", tmp_path)
    cfg = config_loader.load_config()
    assert cfg.AUTONOMY_LEVEL == 4
    assert cfg.REPOS == {"demo": "/ruta/demo"}
    assert "_config_local" not in sys.modules
    assert "config_local" not in sys.modules
    assert sys.path == path_antes


def test_config_local_ignora_claves_bloqueadas(tmp_path, capsys):
    (tmp_path / "config_local.py").write_text(
        'DATA_DIR = "/otro"\nDATA_DIR_ENV = "X"\nAPP_NAME = "y"\nAUTONOMY_LEVEL = 3\n',
        encoding="utf-8",
    )
    values = config_loader._load_local_vars(tmp_path)
    assert values == {"AUTONOMY_LEVEL": 3}
    err = capsys.readouterr().err
    for key in ("DATA_DIR", "DATA_DIR_ENV", "APP_NAME"):
        assert f"se ignora {key};" in err
