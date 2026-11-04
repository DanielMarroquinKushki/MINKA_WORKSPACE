"""Historial único y recortado (ConversationMemory): recorte que conserva citas, cabeceras fuera,
pares user/assistant compartidos entre agentes, prefijo byte-estable, /clear."""

import json

import pytest

from minka.config_loader import config
from minka.core.memory.conversation_memory import (
    ConversationMemory,
    citations_in,
    strip_agent_headers,
    trim_result,
)
from minka.core.orchestrator import planner
from minka.core.orchestrator import router as router_mod
from minka.core.orchestrator.orchestrator import Orchestrator

LONG = (
    "Resumen: el token se valida en el servicio de sesión.\n\n"
    + "Detalle largo. " * 120
    + "\n\nEvidencia:\ninternal/auth/token.go:42 valida la firma\ninternal/auth/session.go:88 renueva\n\n"
    + "Más relleno. " * 120
    + "\n\nConclusión: el punto único es validateToken()."
)


def test_recorte_corto_entero_y_largo_con_citas_y_parrafos():
    assert trim_result("corto con cita a.go:1", 1500) == "corto con cita a.go:1"
    out = trim_result(LONG, 600)
    assert len(out) <= 600
    assert out.startswith("Resumen: el token se valida")
    assert "internal/auth/token.go:42 valida la firma" in out  # línea con cita entera
    assert "internal/auth/session.go:88 renueva" in out
    assert out.rstrip().endswith("Conclusión: el punto único es validateToken().")
    assert "Detalle largo" not in out or out.count("Detalle largo") < 5  # el relleno se acorta


def test_nunca_parte_una_linea_con_cita():
    cites = "\n".join(f"pkg/file{i}.go:{i} explica el paso {i}" for i in range(60))
    out = trim_result("Intro.\n\n" + cites + "\n\nFin.", 400)
    for line in out.splitlines():
        if "pkg/file" in line:
            assert line in cites.splitlines()  # ninguna cita cortada a la mitad


def test_cabeceras_de_agente_se_quitan_al_guardar():
    text = "### [architecture_agent]\nAnálisis.\n\n### [pr_review_agent]\nRevisión a.go:3."
    assert strip_agent_headers(text) == "Análisis.\n\nRevisión a.go:3."
    mem = ConversationMemory()
    turn = mem.add("revisa", ["architecture_agent", "pr_review_agent"], text, True)
    assert "### [" not in turn.result and turn.citations == ["a.go:3"]
    assert citations_in(text) == ["a.go:3"]


def test_as_messages_alterna_y_filtra_turnos_sin_resultado_util():
    mem = ConversationMemory(max_turns=2)
    mem.add("hola", [], "¡Hola!", False)  # $0, no viaja
    mem.add("p1", ["architecture_agent"], "r1 a.go:1", True)
    mem.add("p2", ["architecture_agent"], "⚠️ falló", False)
    mem.add("p3", ["debug_agent"], "r3", True)
    mem.add("p4", ["architecture_agent"], "r4", True)
    msgs = mem.as_messages()
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert [m["content"] for m in msgs] == ["p3", "r3", "p4", "r4"]  # últimos 2 útiles
    assert "Turno 1" in mem.planner_context() and len(mem) == 5
    mem.clear()
    assert mem.as_messages() == [] and len(mem) == 0


class _FakeAgent:
    def __init__(self, name, reply):
        self.name, self.reply = name, reply
        self.seen = []
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1}

    def run(self, task):
        self.seen.append({"task": task, "conversation": json.loads(json.dumps(self.conversation))})
        return self.reply


@pytest.fixture
def orch(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "REPOS", {"pagos": str(tmp_path)}, raising=False)
    # Estos tests comparan el texto de la memoria byte a byte: sin anotar citas ni contexto vivo.
    monkeypatch.setattr(config, "CITATION_CHECK_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "LIVE_CONTEXT_ENABLED", False, raising=False)
    monkeypatch.setattr(router_mod.git_tools, "has_pending_changes", lambda path: None)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("planner no debía ejecutarse"))
    o = Orchestrator()
    o.quiet = True
    agents = {
        "architecture_agent": _FakeAgent("architecture_agent", "El token se valida en internal/auth/token.go:42."),
        "development_agent": _FakeAgent("development_agent", "Cambio propuesto en internal/auth/token.go:42."),
    }
    monkeypatch.setattr(o, "_get_agent", lambda n: agents[n])
    o.fakes = agents
    return o


def test_arregla_eso_llega_a_development_con_el_resultado_previo(orch):
    orch.run("¿dónde se valida el token?")  # info_direct → architecture
    orch.run("arregla eso")
    dev = orch.fakes["development_agent"]
    assert orch.last_router == "followup_change" and len(dev.seen) == 1
    conv = dev.seen[0]["conversation"]
    assert conv == [
        {"role": "user", "content": "¿dónde se valida el token?"},
        {"role": "assistant", "content": "El token se valida en internal/auth/token.go:42."},
    ]
    assert dev.seen[0]["task"] == "arregla eso"  # sin bloque de historial en la tarea


def test_prefijo_byte_estable_entre_dos_runs_y_compartido_entre_agentes(orch):
    arch = orch.fakes["architecture_agent"]
    orch.run("¿dónde se valida el token?")
    orch.run("¿y en el otro repo?")  # hereda architecture
    first, second = arch.seen[0]["conversation"], arch.seen[1]["conversation"]
    assert first == []
    assert json.dumps(second[: len(first)]) == json.dumps(first)  # mismo prefijo, bytes iguales
    assert second[-1]["content"].startswith("El token se valida") and second[-2]["content"] == "¿dónde se valida el token?"
    orch.run("arregla eso")
    dev_conv = orch.fakes["development_agent"].seen[0]["conversation"]
    assert dev_conv[:2] == second[:2]  # el historial es el MISMO para development


def test_salida_consolidada_se_muestra_con_cabeceras_pero_se_guarda_sin_ellas(orch, monkeypatch):
    monkeypatch.setattr(
        planner, "_llm_plan",
        lambda task, context="": planner.LLMPlan([
            {"agent": "architecture_agent", "task": "analiza internal/auth/token.go"},
            {"agent": "development_agent", "task": "implementa el cambio en internal/auth/token.go"},
        ]),
    )
    out = orch.run("analiza y luego implementa la validación en internal/auth/token.go")
    assert "### [architecture_agent]" in out and "### [pr_review_agent]" not in out
    orch.run("¿y en el otro repo?")
    for agent in orch.fakes.values():
        for call in agent.seen:
            assert "### [" not in call["task"]
            assert all("### [" not in m["content"] for m in call["conversation"])
    assert "### [" not in orch.memory.last.result or orch.memory.last.request == "¿y en el otro repo?"
    assert all("### [" not in t.result for t in orch.memory.turns)


def test_clear_vacia_la_memoria_y_el_router_no_hereda(orch):
    orch.run("¿dónde se valida el token?")
    orch.clear_session()
    assert len(orch.memory) == 0 and orch.session_turns == []
    out = orch.run("¿y eso?")
    assert orch.last_router == "ambiguous" and "ambiguo" in out


def test_memoria_no_se_persiste_ni_entra_en_metrics(orch, tmp_path):
    orch.run("¿dónde se valida el token?")
    record = orch.last_run_record
    assert "memory" not in record and "history" not in json.dumps(record)
    assert "token.go" not in json.dumps(record)
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert all("token.go:42" not in p.read_text(errors="ignore") for p in files)
