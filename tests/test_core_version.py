"""Guard de CORE_MODULE_NAME vacío en check_core_version / check_version_drift."""

from config_loader import config
from core.tools.analysis.dependency_mapper import check_version_drift


def test_core_module_vacio_no_matchea_ni_reporta_drift(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CORE_MODULE_NAME", "")
    for name, go in (("a", "1.18"), ("b", "1.25")):
        (tmp_path / name).mkdir()
        (tmp_path / name / "go.mod").write_text(f"module x/{name}\n\ngo {go}\n\nrequire foo/bar v1.0.0\n")
    out = check_version_drift({"a": str(tmp_path / "a"), "b": str(tmp_path / "b")})
    assert "VERSION DRIFT" not in out
    assert "=[NO ENCONTRADO EN CÓDIGO]" in out
    assert "no configurado" in out
