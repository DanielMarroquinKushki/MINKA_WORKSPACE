"""Voz (cercana|directa) y contexto de decisiones/alcance en el mensaje user; verificación de
referencias ADR; métricas. Sin red ni modelo."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from minka import main as minka_main
from minka.config_loader import config
from minka.core import metrics, render
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.memory import decisions as dec
from minka.core.memory.session_memory import SessionMemory
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator import response_profile as rp
from minka.core.tools.analysis.citations import RepoIndex

LOG = """# Decisiones

## ADR-001 — Rotar la working key con el endpoint de logon
**Fecha:** 2026-10-01
**Estado:** aceptada
**Contexto:** la WK vence y hay que renovarla.
**Decisión:** rotar la WK llamando a logon al arrancar.
**Consecuencias:** una llamada más por arranque.

## ADR-002 — Guardar la WK en caché local
**Fecha:** 2026-10-05
**Estado:** reemplazada el 2026-10-20 por ADR-003: la caché local no sobrevive al reinicio.
**Contexto:** evitar llamar a logon en cada transacción.
**Decisión:** cachear la WK en memoria del proceso.
**Consecuencias:** riesgo de WK vieja.

## ADR-003 — Guardar la WK en el almacén compartido
**Fecha:** 2026-10-20
**Estado:** aceptada. Revierte ADR-002 y apunta al almacén compartido.
**Contexto:** la caché local se perdía.
**Decisión:** guardar la WK cifrada en el almacén compartido.
**Consecuencias:** una lectura remota por arranque.

## Plantilla (no es una decisión)
**Fecha:** 2026-01-01
"""

ACTIVE = "# Contexto Activo — Demo\n*Ticket: DEMO-123 — Integración del adquirente*\n*Última actualización: 2026-11-10*\n\n## Ticket actual\nx\n\n## Estado (2026-11-10)\n- y\n"


@pytest.fixture
def proyecto(tmp_path, monkeypatch):
    team = tmp_path / "projects" / "demo" / "knowledge" / "team"
    team.mkdir(parents=True)
    (team / "decisions_log.md").write_text(LOG)
    ac = tmp_path / "active-context"
    ac.mkdir()
    (ac / "demo.md").write_text(ACTIVE)
    for key, value in (("PROJECTS_DIR", tmp_path / "projects"), ("ACTIVE_PROJECT", "demo"),
                       ("ACTIVE_CONTEXT_DIR", str(ac)), ("DATA_DIR", tmp_path)):
        monkeypatch.setattr(config, key, value, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# parseo, selección, alcance y bloque
# ---------------------------------------------------------------------------


def test_parse_decisiones_con_reemplazo_en_ambas_direcciones():
    ds = dec.parse_decisions(LOG)
    assert [d.id for d in ds] == ["ADR-001", "ADR-002", "ADR-003"]  # la plantilla no cuenta
    d2 = next(d for d in ds if d.id == "ADR-002")
    assert d2.replaced and d2.replaced_by == "ADR-003" and d2.date == "2026-10-05"
    d3 = next(d for d in ds if d.id == "ADR-003")
    assert not d3.replaced and d3.replaces == ["ADR-002"] and d3.status.startswith("aceptada")
    assert dec.index(ds) == {"ADR-001": "2026-10-01", "ADR-002": "2026-10-05", "ADR-003": "2026-10-20"}


def test_selector_por_terminos_y_siglas(proyecto):
    ds = dec.load_decisions(config, "demo")
    sel = dec.select(ds, "¿cuál es el estado de la rotación de la WK según el código?")
    assert {d.id for d in sel} >= {"ADR-001", "ADR-003"}  # "WK" en texto o título
    assert sel[0].id == "ADR-003"  # WK en el título pesa doble y es más reciente
    assert len(dec.select(ds, "WK", limit=2)) == 2
    assert dec.select(ds, "¿qué hace el parser de TLV?") == []
    assert dec.select(ds, "y eso") == []


def test_bloque_marca_reemplazadas_y_lleva_reglas(proyecto):
    ds = dec.load_decisions(config, "demo")
    sel = dec.select(ds, "caché de la WK")
    block = dec.user_block(sel, dec.load_scope(config, "demo"))
    assert block.startswith(dec.LABEL)
    assert "Alcance del ticket DEMO-123 — Integración del adquirente (estado al 2026-11-10)" in block
    assert "ADR-002 (2026-10-05, reemplazada" in block and "REEMPLAZADA por ADR-003: no usar como base" in block
    assert "ADR-003 (2026-10-20, aceptada)" in block
    assert dec.RULES in block and "no hay decisión registrada" in dec.RULES


def test_sin_historial_y_alcance_indeterminado(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "nada", raising=False)
    monkeypatch.setattr(config, "ACTIVE_CONTEXT_DIR", str(tmp_path / "nada"), raising=False)
    ds = dec.load_decisions(config, "demo")
    scope = dec.load_scope(config, "demo")
    assert ds == [] and not scope.known
    block = dec.user_block([], scope)
    assert dec.NO_HISTORY in block and dec.SCOPE_UNKNOWN in block


def test_tope_de_tokens_recorta_detalle_antes_que_ids(proyecto):
    ds = dec.load_decisions(config, "demo")
    sel = dec.select(ds, "WK logon caché almacén", limit=3)
    full = dec.user_block(sel, dec.load_scope(config, "demo"), max_tokens=600)
    small = dec.user_block(sel, dec.load_scope(config, "demo"), max_tokens=120)
    assert len(small) < len(full) and len(small) <= int(120 * 2.6 * 1.25)
    assert sel[0].id in small and "- ADR-" in small
    assert small.count("\n  ") < full.count("\n  ")  # el detalle es lo primero que se sacrifica


# ---------------------------------------------------------------------------
# voz y perfiles
# ---------------------------------------------------------------------------


def test_voz_por_perfil_y_desactivacion():
    for profile in rp.VOICE_PROFILES:
        assert "Voz cercana" in rp.user_block(profile, rp.VOICE_CERCANA)
        assert "Voz directa" in rp.user_block(profile, rp.VOICE_DIRECTA)
        assert "Voz" not in rp.user_block(profile, None)
    assert rp.user_block(rp.PROFILE_FULL, rp.VOICE_CERCANA) == ""  # --completo/--debug/"en detalle"
    assert rp.voice_block("otra", rp.PROFILE_BRIEF) == ""
    cercana = rp.user_block(rp.PROFILE_BRIEF, rp.VOICE_CERCANA)
    for texto in ("1 a 3 líneas", "🔴", "🟠", "🟡", "UNA recomendación", "No añadas hechos"):
        assert texto in cercana
    directa = rp.user_block(rp.PROFILE_BRIEF, rp.VOICE_DIRECTA)
    assert "sin apertura" in directa and "1 a 3 líneas" not in directa and "UNA recomendación" in directa
    assert cercana.startswith(rp.BRIEF_BLOCK)  # la estructura actual se conserva


@pytest.mark.parametrize("kwargs,esperado", [
    ({}, "cercana"), ({"completo": True}, None), ({"debug": True}, None), ({"plain": True}, None),
])
def test_flag_voice_en_main(monkeypatch, kwargs, esperado):
    monkeypatch.delenv("MINKA_PLAIN", raising=False)
    monkeypatch.setattr(config, "VOICE", "cercana", raising=False)
    base = {"completo": False, "debug": False, "plain": False}
    assert minka_main._voice(**{**base, **kwargs}) == esperado


def test_voice_directa_desde_config_y_valor_invalido(monkeypatch):
    monkeypatch.delenv("MINKA_PLAIN", raising=False)
    monkeypatch.setattr(config, "VOICE", "DIRECTA", raising=False)
    assert minka_main._voice(completo=False, debug=False, plain=False) == "directa"
    monkeypatch.setattr(config, "VOICE", "rara", raising=False)
    assert minka_main._voice(completo=False, debug=False, plain=False) == "cercana"


def test_plain_sin_emojis():
    assert render.strip_voice_emojis("🔴 alto\n🟠 medio y 🟡 bajo") == "alto\nmedio y bajo"


def test_citas_intactas_con_voz():
    reply = "Lo importante: la WK rota en el logon.\n\n- `Rotate()` en pos/wk.go:12 🔴 riesgo\n\nRecomendación: revisar pos/wk.go:12."
    out, offered, _ = rp.strip_closing_offer(reply)
    assert not offered and out.count("pos/wk.go:12") == 2


# ---------------------------------------------------------------------------
# verificador: referencias a decisiones
# ---------------------------------------------------------------------------


def test_referencias_adr_existentes_e_inexistentes(tmp_path):
    known = dec.index(dec.parse_decisions(LOG))
    text = ("Según ADR-001 (2026-10-01) se rota en logon.\n"
            "ADR-002 quedó reemplazada; ADR-099 no existe.\n"
            "ADR-003 (2026-01-01) trae fecha equivocada.")
    r = cc.verify(text, RepoIndex({}), previous=[], decisions=known)
    lines = r.text.split("\n")
    assert lines[0].endswith(cc.MARK_DOC_DECISION)
    assert cc.MARK_DOC_DECISION in lines[1] and lines[1].endswith(f"{cc.MARK_UNSUPPORTED}: ADR-099")
    assert lines[2].endswith(f"{cc.MARK_UNSUPPORTED}: ADR-003 (2026-01-01)")
    assert (r.decisions_checked, r.decisions_unsupported) == (4, 2)


def test_sin_historial_toda_referencia_queda_sin_respaldo():
    r = cc.verify("Ver ADR-001.", RepoIndex({}), previous=[], decisions={})
    assert r.decisions_unsupported == 1 and cc.MARK_UNSUPPORTED in r.text


def test_metricas_llevan_decisiones():
    usage = {"per_agent": [{"agent": "architecture_agent", "model": "claude-haiku-4-5", "requests": 1}], "totals": {}}
    rec = metrics.build_run_record(run_id="r", source="cli", project="p", cwd="~", router="info_direct", plan=[],
                                   fix_loop_iters=0, ok=True, duration_s=1.0, usage=usage,
                                   claims={"decisions_used": 2, "decisions_unsupported": 1})
    assert (rec["decisions_used"], rec["decisions_unsupported"]) == (2, 1)
    agg = metrics.aggregate([rec, rec])
    assert (agg["decisions_used"], agg["decisions_unsupported"]) == (4, 2)


# ---------------------------------------------------------------------------
# agente: system y tools byte-idénticos con y sin voz/decisiones; todo va en el user
# ---------------------------------------------------------------------------


class _Agent(BaseAgent):
    def get_system_prompt(self) -> str:
        return "SYSTEM"


class _noop_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def update(self, **k):
        return None


class _Client:
    def __init__(self):
        self.requests, self.messages = [], self

    def create(self, **kw):
        self.requests.append(copy.deepcopy(kw))
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0, cache_creation=None), model="m")


def _agent(monkeypatch):
    agent = _Agent.__new__(_Agent)
    agent.name = "architecture_agent"
    agent.config = config
    agent.client = _Client()
    agent.memory = SessionMemory(agent_name=agent.name)
    agent.vector_memory = SimpleNamespace(search=lambda *a, **k: [], add=lambda *a, **k: None)
    agent.tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
    agent.knowledge = SimpleNamespace(get_relevant_context=lambda: "KB")
    agent.iteration_count, agent.max_iterations = 0, 5
    agent.last_usage = agent._empty_usage()
    agent.live_context_block, agent.decisions_block, agent.voice, agent.response_profile = "", "", None, None
    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(base_agent, "trace", lambda *a, **k: _noop_ctx())
    monkeypatch.setattr(base_agent.progress, "track", lambda name: _noop_ctx())
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_log", lambda *a, **k: None)
    return agent


def test_system_y_tools_byte_identicos_y_bloques_solo_en_user(monkeypatch, proyecto):
    plain = _agent(monkeypatch)
    plain.run("¿estado de la WK?")
    voiced = _agent(monkeypatch)
    voiced.response_profile, voiced.voice = rp.PROFILE_BRIEF, rp.VOICE_CERCANA
    voiced.decisions_block = dec.user_block(dec.select(dec.load_decisions(config, "demo"), "WK"), dec.load_scope(config, "demo"))
    voiced.run("¿estado de la WK?")
    a, b = plain.client.requests[0], voiced.client.requests[0]
    assert a["system"] == b["system"] and a["tools"] == b["tools"]
    user_a = a["messages"][-1]["content"]
    user_b = b["messages"][-1]["content"]
    text_b = user_b if isinstance(user_b, str) else "".join(c.get("text", "") for c in user_b)
    text_a = user_a if isinstance(user_a, str) else "".join(c.get("text", "") for c in user_a)
    assert "Voz cercana" in text_b and dec.LABEL in text_b and "ADR-001" in text_b
    assert "Voz" not in text_a and dec.LABEL not in text_a
    assert voiced.decisions_block == "" and voiced.voice == rp.VOICE_CERCANA  # bloque de un solo uso
