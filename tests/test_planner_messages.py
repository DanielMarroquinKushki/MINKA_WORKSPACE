"""El bloque de contexto del planner no debe decir "no enrutar" ni invitar a devolver vacío:
con el rótulo viejo el modelo respondía steps=[] (medido con una llamada real)."""

from types import SimpleNamespace

import pytest

from minka.core.orchestrator import planner
from minka.core.orchestrator.planner import CONTEXT_LABEL, _planner_messages


def _texts(messages):
    content = messages[-1]["content"]
    return [content] if isinstance(content, str) else [b["text"] for b in content]


def test_rotulo_no_dice_no_enrutar_y_pide_al_menos_un_paso():
    texts = _texts(_planner_messages("¿dónde se resuelve el token?", "[Turno 1] Dev: analiza X"))
    assert "no enrutar" not in " ".join(texts).lower()
    assert "al menos un paso" in texts[0] and "SOLO el pedido actual" in texts[0]
    assert texts[-1].startswith("Pedido actual:\n¿dónde se resuelve el token?")  # último bloque


def test_sin_contexto_el_pedido_va_solo():
    assert _planner_messages("analiza X", "") == [{"role": "user", "content": "analiza X"}]


class _ModeloQueSeAsustaConNoEnrutar:
    """Simula lo medido: si algún bloque dice "no enrutar", devuelve steps vacíos."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        texts = _texts(kw["messages"])
        steps = [] if any("no enrutar" in t.lower() for t in texts) else [
            {"agent": "architecture_agent", "task": "ubicar dónde se resuelve el token"}
        ]
        block = SimpleNamespace(type="tool_use", name="submit_plan", input={"steps": steps})
        return SimpleNamespace(content=[block], stop_reason="tool_use", usage=None)


@pytest.fixture
def planner_simulado(monkeypatch):
    from minka.core.agents import base_agent

    monkeypatch.setattr(base_agent, "build_llm_client", lambda: _ModeloQueSeAsustaConNoEnrutar())
    monkeypatch.setattr(base_agent, "resolve_model_id", lambda m: m)
    monkeypatch.setattr(planner, "load_system_prompt", lambda name: "prompt")


def test_con_el_rotulo_actual_el_planner_simulado_da_un_paso(planner_simulado):
    res = planner._llm_plan("¿dónde se resuelve el token?", "[Turno 1] Dev: analiza el flujo de pagos")
    assert res.error is None and [s["agent"] for s in res.steps] == ["architecture_agent"]
    assert "no enrutar" not in CONTEXT_LABEL.lower()
