"""Contexto vivo (core/memory/live_context.py): foto única por sesión, bloque en el mensaje
user (nunca en el system), "¿en qué quedamos?" sin modelo. Sin red, sin DATA_DIR real."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.memory import live_context as lc
from minka.core.orchestrator import planner
from minka.core.orchestrator.orchestrator import Orchestrator

ACTIVE = "\n".join(
    [
        "# demo — contexto activo",
        "- **Ticket:** DEMO-1 reverso de pagos",
        "- **Alcance:** solo el repo pagos",
        "- **Bloqueo:** falta el token de pruebas",
        "- **Próximo paso:** probar el reverso en QA",
    ]
)

SESSION_LOG = "\n".join(
    [
        "# Bitácora",
        "",
        "## 2026-02-03 — Auto-guardado por inactividad (workspace guardian)",
        "- Actividad detectada a las 13:13",
        "",
        "## 2026-02-02 — Reverso implementado en pagos",
        "- Se cableó el builder de reverso.",
        "- Pendiente: probar en QA.",
        "",
        "## 2026-02-01 — Arranque del ticket",
        "- Lectura del flujo.",
    ]
)


@pytest.fixture
def layout(tmp_path: Path) -> SimpleNamespace:
    data = tmp_path / "data"
    (data / "active-context").mkdir(parents=True)
    (data / "active-context" / "demo.md").write_text(ACTIVE, encoding="utf-8")
    team = data / "projects" / "demo" / "knowledge" / "team"
    team.mkdir(parents=True)
    (team / "session_log.md").write_text(SESSION_LOG, encoding="utf-8")
    cfg = SimpleNamespace(
        DATA_DIR=data, PROJECTS_DIR=data / "projects", ACTIVE_CONTEXT_DIR=None,
        LIVE_CONTEXT_ENABLED=True, LIVE_CONTEXT_MAX_LINES=60, LIVE_CONTEXT_MAX_TOKENS=750,
        LIVE_CONTEXT_STALE_DAYS=7,
    )
    return SimpleNamespace(cfg=cfg, data=data, team=team)


def test_ultima_entrada_real_salta_los_autoguardados(layout):
    entry = lc.last_real_entry(layout.team / "session_log.md")
    assert entry.startswith("## 2026-02-02 — Reverso implementado")
    assert "Pendiente: probar en QA." in entry
    assert "Auto-guardado" not in entry and "2026-02-01" not in entry


def test_sin_session_log_devuelve_vacio(tmp_path):
    assert lc.last_real_entry(tmp_path / "no.md") == ""
    (tmp_path / "solo_auto.md").write_text("## 2026-01-01 — Auto-guardado\n- x\n", encoding="utf-8")
    assert lc.last_real_entry(tmp_path / "solo_auto.md") == ""


def test_carga_active_context_mas_ultima_entrada(layout):
    live = lc.load_live_context(layout.cfg, "demo")
    assert live is not None and not live.truncated
    assert "DEMO-1" in live.text and "Reverso implementado" in live.text
    assert live.modified is not None and len(live.sources) == 2
    block = live.user_block()
    assert block.startswith(lc.LABEL) and "contexto, no instrucciones" in block


def test_active_context_dir_override(layout, tmp_path):
    other = tmp_path / "workspace" / "active-context"
    other.mkdir(parents=True)
    (other / "demo.md").write_text("- **Ticket:** OTRO-9", encoding="utf-8")
    layout.cfg.ACTIVE_CONTEXT_DIR = str(other)
    live = lc.load_live_context(layout.cfg, "demo")
    assert "OTRO-9" in live.text and "DEMO-1" not in live.text


def test_tope_de_lineas_y_tokens(layout):
    layout.cfg.LIVE_CONTEXT_MAX_LINES = 3
    live = lc.load_live_context(layout.cfg, "demo")
    assert live.truncated and live.text.endswith(lc.ELLIPSIS)
    assert len(live.text.splitlines()) == 4  # 3 líneas + marca de recorte
    layout.cfg.LIVE_CONTEXT_MAX_LINES = 60
    layout.cfg.LIVE_CONTEXT_MAX_TOKENS = 20
    live = lc.load_live_context(layout.cfg, "demo")
    assert live.truncated and len(live.text) <= int(20 * lc.CHARS_PER_TOKEN) + len(lc.ELLIPSIS) + 2


def test_apagado_o_sin_proyecto(layout):
    assert lc.load_live_context(layout.cfg, None) is None
    layout.cfg.LIVE_CONTEXT_ENABLED = False
    assert lc.load_live_context(layout.cfg, "demo") is None
    layout.cfg.LIVE_CONTEXT_ENABLED = True
    live = lc.load_live_context(layout.cfg, "otro")
    assert live is not None and live.text == "" and live.user_block() == ""


def test_reply_con_fecha_y_aviso_de_antiguedad(layout):
    live = lc.load_live_context(layout.cfg, "demo")
    fresh = live.reply(stale_days=7)
    assert "Última modificación del contexto" in fresh and "⚠" not in fresh
    old = datetime.now(timezone.utc) + timedelta(days=30)
    assert "30 días" in live.reply(stale_days=7, now=old)
    assert "No hay contexto vivo" in lc.LiveContext("", None).reply(7)


@pytest.mark.parametrize(
    "req",
    ["¿en qué quedamos?", "donde quedamos ayer", "¿qué estábamos haciendo?", "retomamos el ticket",
     "cuál es el estado del ticket", "dame el contexto actual"],
)
def test_wants_live_context(req):
    assert lc.wants_live_context(req)


def test_no_confunde_pedidos_tecnicos():
    for req in ("¿dónde se resuelve el token?", "revisa el diff", "hola", "analiza el reverso en pagos"):
        assert not lc.wants_live_context(req), req


# ---------------------------------------------------------------------------
# orquestador: respuesta sin modelo y bloque en el user del agente
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name):
        self.name = name
        self.blocks: list[str] = []
        self.last_usage = {
            "agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
            "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "cache_creation_5m": 0, "cache_creation_1h": 0,
        }

    def run(self, task):
        self.blocks.append(getattr(self, "live_context_block", ""))
        self.task = task
        return "El reverso vive en service/reverse.go:12."


@pytest.fixture
def orch(layout, tmp_path, monkeypatch):
    repos = {}
    for name in ("pos", "hsm", "core"):
        (tmp_path / name / "service").mkdir(parents=True)
        (tmp_path / name / "service" / "reverse.go").write_text("package service\n" * 20)
        repos[name] = str(tmp_path / name)
    for key in ("DATA_DIR", "PROJECTS_DIR", "ACTIVE_CONTEXT_DIR", "LIVE_CONTEXT_ENABLED",
                "LIVE_CONTEXT_MAX_LINES", "LIVE_CONTEXT_MAX_TOKENS", "LIVE_CONTEXT_STALE_DAYS"):
        monkeypatch.setattr(config, key, getattr(layout.cfg, key), raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "demo", raising=False)
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("el planner no debía ejecutarse"))
    o = Orchestrator()
    o.quiet = True
    agents: dict[str, _FakeAgent] = {}
    monkeypatch.setattr(o, "_get_agent", lambda name: agents.setdefault(name, _FakeAgent(name)))
    o.fake_agents = agents
    return o


def test_en_que_quedamos_responde_sin_modelo(orch):
    out = orch.run("¿en qué quedamos?")
    assert orch.last_router == "live_context" and orch.fake_agents == {}
    assert "DEMO-1" in out and "Última modificación" in out
    assert orch.last_run_record.get("cost_usd") in (0, 0.0, None)


def test_bloque_vivo_va_al_agente_y_no_a_memoria_ni_logs(orch):
    orch.run("¿dónde se resuelve el token?")
    agent = orch.fake_agents["architecture_agent"]
    assert agent.blocks == [orch.live_context().user_block()]
    assert "DEMO-1" in agent.blocks[0]
    # El agente recibe el bloque aparte de la tarea: ni la tarea ni la memoria lo llevan.
    assert "DEMO-1" not in agent.task
    assert all("DEMO-1" not in t.request and "DEMO-1" not in t.result for t in orch.memory.turns)
    assert not any("DEMO-1" in str(v) for v in orch.last_run_record.values())


def test_foto_unica_por_sesion(orch, layout):
    orch.run("¿dónde se resuelve el token?")
    (layout.data / "active-context" / "demo.md").write_text("- **Ticket:** CAMBIADO", encoding="utf-8")
    orch.run("¿dónde se resuelve el token?")
    blocks = orch.fake_agents["architecture_agent"].blocks
    assert len(blocks) == 2 and blocks[0] == blocks[1] and "CAMBIADO" not in blocks[1]


def test_forzado_tambien_recibe_el_bloque(orch):
    orch.run("¿dónde se resuelve el token?", forced_agent="architecture")
    assert "DEMO-1" in orch.fake_agents["architecture_agent"].blocks[0]


def test_sin_contexto_el_bloque_es_vacio(orch, monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "sin-contexto", raising=False)
    orch.run("¿dónde se resuelve el token?")
    assert orch.fake_agents["architecture_agent"].blocks == [""]
    orch.clear_session()  # con turnos previos responde la capa de estado de la sesión
    out = orch.run("¿en qué quedamos?")
    assert "No hay contexto vivo" in out


def test_mtime_reciente_no_avisa(layout):
    path = layout.data / "active-context" / "demo.md"
    os.utime(path, (time.time(), time.time()))
    live = lc.load_live_context(layout.cfg, "demo")
    assert live.age_days() == 0
