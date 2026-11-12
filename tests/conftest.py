"""Fixtures compartidas: DATA_DIR aislado y proyecto demo en tmp_path (sin datos reales)."""

import json
import os
import pathlib
import shutil
import tempfile

from minka._app import APP_NAME, DATA_DIR_ENV

# Hermeticidad: la suite no debe ver el entorno del dev. Se guarda el entorno original, se
# borran MINKA_*, AWS_* y ANTHROPIC_API_KEY, HOME pasa a un temporal y DATA_DIR (MINKA_HOME) a otro.
# Debe hacerse ANTES del primer import de config, que calcula DATA_DIR y carga el .env al importarse.
# Todo se restaura en pytest_sessionfinish.
_ORIGINAL_ENV = dict(os.environ)
_ENV_PREFIXES = (f"{APP_NAME.upper()}_", "AWS_")
_ENV_EXACT = ("ANTHROPIC_API_KEY",)
for _key in list(os.environ):
    if _key.startswith(_ENV_PREFIXES) or _key in _ENV_EXACT:
        del os.environ[_key]
_TEST_HOME = tempfile.mkdtemp(prefix=f"{APP_NAME}-home-")
os.environ["HOME"] = _TEST_HOME
_TEST_DATA_DIR = tempfile.mkdtemp(prefix=f"{APP_NAME}-tests-")
os.environ[DATA_DIR_ENV] = _TEST_DATA_DIR

import pytest  # noqa: E402

from minka import config_loader  # noqa: E402
from minka.config_loader import config  # noqa: E402
from minka.core.orchestrator import approval_gate as ag  # noqa: E402

# Los tests del motor son genéricos: el proyecto de referencia vive en DATA_DIR, fuera del
# repo, así que aquí no hay fixtures por proyecto real (ver proyecto demo más abajo).

# Las 7 claves del proyecto demo (valores de ejemplo, sin datos reales).
DEMO_VALUES = {
    "CORE_MODULE_NAME": "demo-core",
    "TEAM_PILLARS": [
        "  01 Simplicidad — ¿es la solución más simple?",
        "  02 Claridad    — ¿se entiende sin preguntar?",
    ],
    "TEAM_PILLARS_HEADING": "Pilares Demo",
    "TEAM_PILLARS_READABILITY": "Pilar Claridad",
    "TEAM_PILLARS_LINE_CAP_HINT": "Pilar Simplicidad: divide el cambio.",
    "RISK_PATTERNS": [
        ("HARDCODED_TOKEN", "CRÍTICA", r"0123456789ABCDEF", "Llave de ejemplo del demo"),
        ("DEMO_FIXME", "MEDIA", r"FIXME-DEMO", "Marcador FIXME-DEMO sin resolver"),
    ],
    "PR_DIFF_ALERTS": [
        ("0123456789abcdef", "⚠️ Llave de ejemplo del demo."),
        ("fixme-demo", "ℹ️ FIXME-DEMO en el diff."),
    ],
}
DEMO_PR_PROMPT = 'PR_REVIEW_SYSTEM_PROMPT = "Agente de Revisión de PRs (demo)."\n'
DEMO_CASES = {
    "cases": [
        {
            "id": "demo-1",
            "agent": "architecture_agent",
            "input": "¿Qué hace el demo?",
            "must_mention": ["demo"],
        }
    ]
}


def write_demo_project(base: pathlib.Path) -> pathlib.Path:
    """Crea base/projects/demo/ (config.py, prompts/, evals/) y devuelve su ruta."""
    proj = base / "projects" / "demo"
    (proj / "prompts").mkdir(parents=True)
    (proj / "evals").mkdir()
    lines = [f"{key} = {value!r}" for key, value in DEMO_VALUES.items()]
    (proj / "config.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (proj / "prompts" / "pr_review_prompt.py").write_text(DEMO_PR_PROMPT, encoding="utf-8")
    cases = json.dumps(DEMO_CASES, ensure_ascii=False)
    (proj / "evals" / "cases.json").write_text(cases, encoding="utf-8")
    return proj


def load_demo(base: pathlib.Path):
    """Carga el demo como namespace nuevo (no toca el config global)."""
    cfg = config_loader.load_config("demo", projects_dir=base / "projects")
    # config_local.py se aplica al final en load_config: re-aplica el demo por encima.
    for key, value in DEMO_VALUES.items():
        setattr(cfg, key, value)
    return cfg


def apply_demo_global(cfg, mp: pytest.MonkeyPatch) -> None:
    """Aplica el demo al config global (temporal: monkeypatch lo restaura)."""
    for key in ("PROJECTS_DIR", "ACTIVE_PROJECT", *DEMO_VALUES):
        mp.setattr(config, key, getattr(cfg, key), raising=False)


def run_gate(mp: pytest.MonkeyPatch, cfg, new_content: str) -> dict:
    """Corre ApprovalGate.request y devuelve los paneles construidos, por título."""
    out = []
    mp.setattr(ag, "Panel", lambda r, **kw: out.append({"renderable": r, **kw}))
    mp.setattr(ag.Confirm, "ask", staticmethod(lambda *a, **k: False))
    mp.setattr(ag.console, "print", lambda *a, **k: None)
    with tempfile.TemporaryDirectory() as td:
        f = pathlib.Path(td) / "x.txt"
        f.write_text("a\n")
        ag.ApprovalGate(cfg).request(str(f), new_content, "fixture")
    return {p["title"]: p for p in out}


@pytest.fixture(autouse=True)
def _sin_inferencia_de_proyecto(request, monkeypatch):
    """La CLI infiere el proyecto por cwd (core/project_resolver.py); en la suite el cwd es el
    repo del motor y no hay DEFAULT_PROJECT, así que por defecto se desactiva la inferencia y
    `--project` sigue funcionando. Los tests marcados `project_inference` usan la real."""
    if request.node.get_closest_marker("project_inference"):
        return
    from minka import main as _main

    def passthrough(explicit, *, debug=False, quiet=False, required=True):
        if explicit:
            config_loader.set_project(explicit)
            config.PROJECT_ORIGIN = "--project"
        return explicit

    monkeypatch.setattr(_main, "_activate_project", passthrough)


@pytest.fixture
def demo_project(tmp_path, monkeypatch):
    base = tmp_path / "dev-agent"
    write_demo_project(base)
    return load_demo(base)


@pytest.fixture
def demo_global(demo_project, monkeypatch):
    apply_demo_global(demo_project, monkeypatch)
    return demo_project


@pytest.fixture
def capture_panels(monkeypatch):
    return lambda cfg, new_content: run_gate(monkeypatch, cfg, new_content)


@pytest.fixture(autouse=True)
def _entorno_hermetico(monkeypatch):
    """Cada test arranca sin MINKA_* (salvo el DATA_DIR de la suite), AWS_* ni ANTHROPIC_API_KEY,
    aunque otro test o el shell del dev las hayan dejado."""
    for key in list(os.environ):
        if key == DATA_DIR_ENV:
            continue
        if key.startswith(_ENV_PREFIXES) or key in _ENV_EXACT:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(DATA_DIR_ENV, _TEST_DATA_DIR)
    monkeypatch.setenv("HOME", _TEST_HOME)


def pytest_sessionfinish(session, exitstatus):
    os.environ.clear()
    os.environ.update(_ORIGINAL_ENV)
    shutil.rmtree(_TEST_DATA_DIR, ignore_errors=True)
    shutil.rmtree(_TEST_HOME, ignore_errors=True)
