"""End-to-end sin planner simulado: git real sobre 3 repos temporales (pos, hsm, core).

Reproduce la secuencia de sesión: "revisa este diff" → "arregla el bug" → "¿y eso?" →
consulta de código → "¿y en el otro repo?". El planner no debe ejecutarse en ninguno de
estos pedidos (si lo hiciera, la prueba falla con AssertionError). Los agentes son falsos:
no hay LLM en ninguna parte.
"""

import subprocess
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core.orchestrator import planner
from minka.core.orchestrator.orchestrator import Orchestrator


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _make_repo(base, name):
    repo = base / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "main.go").write_text("package main\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class _FakeAgent:
    def __init__(self, name, reply="El token se resuelve en el servicio de sesión."):
        self.name = name
        self.reply = reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0}

    def run(self, task):
        self.task = task
        return self.reply


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    repos = {name: str(_make_repo(tmp_path, name)) for name in ("pos", "hsm", "core")}
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(config, "EVALUATOR_ENABLED", False)
    monkeypatch.setattr(config, "META_AGENT_ENABLED", False)
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "out")
    monkeypatch.setattr(config, "REPOS", repos, raising=False)
    monkeypatch.setattr(planner, "_llm_plan", lambda *a, **k: pytest.fail("el planner no debía ejecutarse"))
    orch = Orchestrator()
    state = SimpleNamespace(orch=orch, repos=repos, ran=[], agents={}, lines=[])

    def get_agent(name):
        state.ran.append(name)
        state.agents[name] = _FakeAgent(name)
        return state.agents[name]

    monkeypatch.setattr(orch, "_get_agent", get_agent)
    monkeypatch.setattr(orch, "_stderr", SimpleNamespace(print=lambda text, **kw: state.lines.append(text)))
    return state


def _cost0(orch):
    record = orch.last_run_record
    return record["tokens"]["requests"] == 0 and record["cost_usd"] == 0.0


def test_secuencia_de_sesion_con_repos_limpios(e2e):
    orch = e2e.orch

    out = orch.run("revisa este diff")
    assert orch.last_router == "no_changes" and e2e.ran == [] and _cost0(orch)
    assert "No hay cambios en pos, hsm, core" in out

    out = orch.run("arregla el bug")
    assert orch.last_router == "ambiguous" and e2e.ran == [] and _cost0(orch) and "ambiguo" in out

    out = orch.run("¿y eso?")
    assert orch.last_router == "ambiguous" and e2e.ran == [] and _cost0(orch)

    orch.run("¿dónde se resuelve el token?")
    assert orch.last_router == "info_direct" and e2e.ran == ["architecture_agent"]
    assert orch.last_run_record["tokens"]["requests"] == 1

    orch.run("¿y en el otro repo?")
    assert orch.last_router == "inherit" and e2e.ran == ["architecture_agent", "architecture_agent"]
    assert "→ architecture (hereda turno previo)" in e2e.lines[-1]


def test_repos_sucios_limitan_pr_review_y_no_se_heredan(e2e, tmp_path):
    orch = e2e.orch
    (tmp_path / "hsm" / "main.go").write_text("package main\n// cambio\n")

    orch.run("revisa este diff")
    assert orch.last_router == "review_direct" and e2e.ran == ["pr_review_agent"]
    task = e2e.agents["pr_review_agent"].task
    assert "Repos con cambios pendientes: hsm." in task
    assert "Los repos pos, core están limpios: no ejecutes show_diff sobre ellos." in task
    assert "→ pr_review (cambios pendientes en hsm, directo)" in e2e.lines[-1]

    out = orch.run("¿y eso?")  # pr_review nunca se hereda
    assert orch.last_router == "ambiguous" and "ambiguo" in out and len(e2e.ran) == 1


def test_rama_citada_revisa_lo_citado_aunque_todo_este_limpio(e2e):
    orch = e2e.orch
    orch.run("revisa el diff de la rama feature/login en pos", use_llm_planner=False)
    assert orch.last_router == "keyword" and e2e.ran == ["pr_review_agent"]


def test_resultado_no_util_no_se_hereda(e2e, monkeypatch):
    orch = e2e.orch
    monkeypatch.setattr(orch, "_get_agent", lambda name: _FakeAgent(name, reply="⚠️ No pude analizarlo."))
    orch.run("¿dónde se resuelve el token?")
    assert orch.session_turns[-1]["useful"] is False
    out = orch.run("¿y en el otro repo?")
    assert orch.last_router == "ambiguous" and "ambiguo" in out
