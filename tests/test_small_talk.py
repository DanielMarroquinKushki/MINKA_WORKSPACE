"""Saludos y agradecimientos: respuesta fija, sin planner, agentes ni sesión AWS (capa 1 del router)."""

import pytest

from minka.config_loader import config
from minka.core.orchestrator import router as router_mod
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.orchestrator.router import CURRENT_REQUEST_MARKER


class _PlanLlamado(Exception):
    pass


@pytest.fixture
def orch(monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", True)
    monkeypatch.setattr(config, "get_aws_session", lambda: pytest.fail("no debía tocar AWS"))
    o = Orchestrator(config=config)
    o.quiet = True
    monkeypatch.setattr(o, "_get_agent", lambda name: pytest.fail(f"no debía crear {name}"))
    return o


@pytest.mark.parametrize("texto", ["hola", "Gracias!", "¡Buenos días!", "ok"])
def test_saludo_no_llama_planner_ni_agentes(orch, monkeypatch, texto):
    monkeypatch.setattr(router_mod, "plan_with_router", lambda *a, **k: pytest.fail("no debía planear"))
    reply = orch.run(texto)
    assert "Agentes disponibles: architecture, development, pr_review" in reply


def test_saludo_tras_historial_interactivo(orch, monkeypatch):
    monkeypatch.setattr(router_mod, "plan_with_router", lambda *a, **k: pytest.fail("no debía planear"))
    task = f"Historial de esta sesión:\n[Turno 1] Dev: analiza X\n---\n\n{CURRENT_REQUEST_MARKER}\nhola"
    assert "Agentes disponibles" in orch.run(task)


def test_saludo_con_pedido_si_pasa_al_planner(monkeypatch):
    monkeypatch.setattr(config, "USE_BEDROCK", False)

    def plan(*a, **k):
        raise _PlanLlamado

    monkeypatch.setattr(router_mod, "plan_with_router", plan)
    o = Orchestrator(config=config)
    o.quiet = True
    with pytest.raises(_PlanLlamado):
        o.run("hola, analiza X", resume=False)
