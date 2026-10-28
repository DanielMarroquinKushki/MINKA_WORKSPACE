"""Pilares y módulo core por proyecto, con el proyecto demo (sin datos reales)."""

import hashlib
import json
import pathlib

from minka.config_loader import config
from minka.core.evaluator.prompt_optimizer import _meta_system
from minka.core.orchestrator.four_r_review import LENS_PROMPTS, _resolve_lens
from minka.core.tools.registry import _TOOLS

FIX = pathlib.Path(__file__).parent / "fixtures" / "demo"
SHA = {
    "approval_gate_pillars_panel.json": (
        "b442a4a20d084581de5358400f0371958d8eec192c0a48ef83912938bdc17959"
    ),
    "approval_gate_linecap_panel.json": (
        "ab53e136936319380d940e3fdfb2fb37ded03944721c220f681bed892d152f59"
    ),
    "four_r_legibilidad_prompt.txt": (
        "1a32ba9bbe987e45b96d597200a03ca163e5f32092dd8a03f8fd3f593ff6d8ae"
    ),
    "prompt_optimizer_meta_system.txt": (
        "662901b1f2d38ddccaad55c6921a959bc0ad07d32f631ed1a4fc0bf893241a26"
    ),
    "check_core_version_description.txt": (
        "72eee03a6491efc703c2eb258d7a3d75414e5f5cdfd365b5b7397c85af384f77"
    ),
}
BIG = "".join(f"l{i}\n" for i in range(200))


def _fixture(name: str) -> str:
    raw = (FIX / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA[name]
    return raw.decode("utf-8")


def _dump(panel: dict, keys: tuple) -> str:
    got = {k: list(panel[k]) if k == "padding" else panel[k] for k in keys}
    return json.dumps(got, ensure_ascii=False, indent=2) + "\n"


def render_pillars_panel(capture_panels, cfg) -> str:
    p = capture_panels(cfg, "b\n")[f"[cyan]{cfg.TEAM_PILLARS_HEADING}[/cyan]"]
    return _dump(p, ("renderable", "title", "border_style", "padding"))


def render_line_cap_panel(capture_panels, cfg) -> str:
    p = capture_panels(cfg, BIG)["[yellow]Line Cap[/yellow]"]
    return _dump(p, ("renderable", "title", "border_style"))


def render_four_r() -> str:
    return _resolve_lens(LENS_PROMPTS["Legibilidad"])


def render_core_version_description() -> str:
    return _TOOLS["check_core_version"].to_anthropic()["description"]


def test_pillars_panel_omitido_sin_team_pillars(monkeypatch, capture_panels):
    monkeypatch.setattr(config, "TEAM_PILLARS", [])
    assert list(capture_panels(config, "b\n")) == ["Gate de aprobación"]


def test_pillars_panel_demo_igual_fixture(capture_panels, demo_project):
    got = render_pillars_panel(capture_panels, demo_project)
    assert got == _fixture("approval_gate_pillars_panel.json")


def test_line_cap_demo_igual_fixture(monkeypatch, capture_panels, demo_project):
    monkeypatch.setattr(demo_project, "LINE_CAP", 150)
    got = render_line_cap_panel(capture_panels, demo_project)
    assert got == _fixture("approval_gate_linecap_panel.json")


def test_line_cap_hint_omitido(monkeypatch, capture_panels, demo_project):
    monkeypatch.setattr(demo_project, "LINE_CAP", 150)
    monkeypatch.setattr(demo_project, "TEAM_PILLARS_LINE_CAP_HINT", "")
    body = capture_panels(demo_project, BIG)["[yellow]Line Cap[/yellow]"]["renderable"]
    assert body.endswith("pasos más pequeños.")


def test_four_r_legibilidad_demo_igual_fixture(demo_global):
    assert render_four_r() == _fixture("four_r_legibilidad_prompt.txt")


def test_four_r_readability_vacia(monkeypatch, demo_global):
    monkeypatch.setattr(config, "TEAM_PILLARS_READABILITY", "")
    vacio = render_four_r()
    assert "(" not in vacio.split(" del sistema")[0]
    assert "LEGIBILIDAD del sistema" in vacio


def test_meta_system_demo_igual_fixture(demo_project):
    got = _meta_system(demo_project.TEAM_PILLARS_HEADING)
    assert got == _fixture("prompt_optimizer_meta_system.txt")


def test_core_version_description_demo_igual_fixture(demo_global):
    desc = render_core_version_description()
    assert isinstance(desc, str)
    assert desc == _fixture("check_core_version_description.txt")


def test_core_version_fallback(monkeypatch, demo_global):
    monkeypatch.setattr(config, "CORE_MODULE_NAME", "")
    assert render_core_version_description() == "Versión de módulo core y de Go en el go.mod."
