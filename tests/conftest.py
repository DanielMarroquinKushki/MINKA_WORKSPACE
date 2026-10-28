"""Fixtures compartidas: proyecto demo en tmp_path y exclusión de *_amex.py sin amex-colombia."""

import json
import pathlib
import tempfile

import pytest

import config_loader
from config_loader import config
from core.orchestrator import approval_gate as ag

_DEV_AGENT = pathlib.Path(__file__).resolve().parent.parent
# Sin projects/amex-colombia/ (repo del motor genérico) los tests byte a byte de amex no aplican.
collect_ignore_glob = [] if (_DEV_AGENT / "projects" / "amex-colombia").is_dir() else ["*_amex.py"]

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
