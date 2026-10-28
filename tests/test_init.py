"""minka --init: copia las plantillas de datos a DATA_DIR sin sobrescribir nunca."""

from pathlib import Path

from minka.config_loader import config
from minka.core.project_scaffold import init_data_dir


def test_init_copia_plantillas_y_no_sobrescribe(tmp_path, monkeypatch):
    data_dir = tmp_path / "home"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    base = Path(config.BASE_DIR)

    r = init_data_dir()
    local, env = data_dir / "config_local.py", data_dir / ".env"
    assert r["created"] == [str(local), str(env)]
    assert r["skipped"] == []
    assert local.read_bytes() == (base / "config.example.py").read_bytes()
    assert env.read_bytes() == (base / ".env.example").read_bytes()

    local.write_text("AUTONOMY_LEVEL = 4\n", encoding="utf-8")
    r = init_data_dir()
    assert r["created"] == []
    assert r["skipped"] == [str(local), str(env)]
    assert local.read_text(encoding="utf-8") == "AUTONOMY_LEVEL = 4\n"
