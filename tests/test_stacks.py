"""STACK y COMPLIANCE (core/stacks.py): perfiles por lenguaje, tools exclusivas de Go, PCI
opcional, knowledge por defecto y la prueba de aceptación con un repo Python temporal.
Regla de oro: sin STACK ni COMPLIANCE todo queda byte a byte como antes."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from minka.config_loader import config
from minka.core import stacks
from minka.core.knowledge.loader import KnowledgeLoader
from minka.core.tools.analysis import risk_analyzer
from minka.core.tools.filesystem import read_tools, search_tools
from minka.core.tools.rag import indexer
from minka.core.tools.registry import ToolRegistry

TEMPLATE_TOOLS = {
    "architecture_agent": [
        "read_file", "scan_directory", "list_files", "grep_code", "find_interface",
        "find_usages", "parse_interfaces", "detect_patterns", "check_core_version",
        "map_dependencies", "check_version_drift", "detect_risks",
    ],
    "pr_review_agent": [
        "read_file", "show_diff", "git_log", "grep_code", "find_usages", "detect_risks",
        "check_pci_compliance", "check_version_drift",
    ],
}


def _set(monkeypatch, **values):
    """Simula un proyecto: AGENT_TOOLS de la plantilla + lo que se pase (STACK, COMPLIANCE…)."""
    base = {"AGENT_TOOLS": TEMPLATE_TOOLS, "AGENT_KNOWLEDGE": {}, "RISK_PATTERNS": [], "STACK": None, "COMPLIANCE": None}
    base.update(values)
    for key, value in base.items():
        monkeypatch.setattr(config, key, value, raising=False)


# ---------------------------------------------------------------------------
# perfiles
# ---------------------------------------------------------------------------


def test_stack_ausente_es_go_y_esquema_viejo(monkeypatch):
    _set(monkeypatch)
    assert stacks.stack_name() == "go" and not stacks.stack_declared()
    assert stacks.compliance() == {"pci"}
    assert stacks.profile().ext == ".go" and stacks.profile().glob == "*.go"
    assert stacks.localize("ej '.go' y '*.go'") == "ej '.go' y '*.go'"


def test_stack_declarado_sin_compliance_no_tiene_pci(monkeypatch):
    _set(monkeypatch, STACK="go")
    assert stacks.compliance() == frozenset()
    _set(monkeypatch, STACK="python", COMPLIANCE=["PCI"])
    assert stacks.compliance() == {"pci"} and stacks.profile().ext == ".py"
    _set(monkeypatch, STACK="desconocido")
    assert stacks.stack_name() == "go"  # fallback seguro


def test_localize_y_code_files(tmp_path, monkeypatch):
    _set(monkeypatch, STACK="node")
    assert stacks.localize("Glob, ej '*.go'. Extensión, ej '.go'.") == "Glob, ej '*.ts'. Extensión, ej '.ts'."
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.ts").write_text("x", encoding="utf-8")
    (tmp_path / "src" / "b.js").write_text("x", encoding="utf-8")
    (tmp_path / "node_modules" / "m").mkdir(parents=True)
    (tmp_path / "node_modules" / "m" / "c.js").write_text("x", encoding="utf-8")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "d.js").write_text("x", encoding="utf-8")
    assert sorted(p.name for p in stacks.code_files(tmp_path)) == ["a.ts", "b.js"]


def test_code_files_go_igual_que_antes(tmp_path, monkeypatch):
    _set(monkeypatch)
    for rel in ("a.go", "build/b.go", "vendor/v.go", "mocks/m.go"):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("package x\n", encoding="utf-8")
    assert sorted(p.name for p in stacks.code_files(tmp_path)) == ["a.go", "b.go", "m.go"]  # todo salvo vendor/
    assert sorted(p.name for p in stacks.code_files(tmp_path, extra_skip=("mocks",))) == ["a.go", "b.go"]


# ---------------------------------------------------------------------------
# tools por stack y compliance
# ---------------------------------------------------------------------------


def test_tools_sin_stack_identicas_a_las_de_siempre(monkeypatch):
    _set(monkeypatch)
    names = [t["name"] for t in ToolRegistry.get_tools_for_agent("architecture_agent")]
    assert names == TEMPLATE_TOOLS["architecture_agent"] + ["list_repos"]
    pr = [t["name"] for t in ToolRegistry.get_tools_for_agent("pr_review_agent")]
    assert "check_pci_compliance" in pr
    schema = json.dumps(ToolRegistry.get_tools_for_agent("architecture_agent"))
    assert "'.go'" in schema and "'*.go'" in schema  # ejemplos intactos


def test_tools_python_sin_go_ni_pci_y_localizadas(monkeypatch):
    _set(monkeypatch, STACK="python")
    names = [t["name"] for t in ToolRegistry.get_tools_for_agent("architecture_agent")]
    assert not (set(names) & stacks.GO_ONLY_TOOLS)
    assert names == ["read_file", "scan_directory", "list_files", "grep_code", "find_usages", "detect_risks", "list_repos"]
    pr = [t["name"] for t in ToolRegistry.get_tools_for_agent("pr_review_agent")]
    assert "check_pci_compliance" not in pr and "check_version_drift" not in pr
    schema = json.dumps(ToolRegistry.get_tools_for_agent("architecture_agent"))
    assert ".go" not in schema and "'.py'" in schema and "'*.py'" in schema


def test_compliance_pci_devuelve_la_tool(monkeypatch):
    _set(monkeypatch, STACK="python", COMPLIANCE=["pci"])
    pr = [t["name"] for t in ToolRegistry.get_tools_for_agent("pr_review_agent")]
    assert "check_pci_compliance" in pr and "check_version_drift" not in pr


# ---------------------------------------------------------------------------
# knowledge por defecto
# ---------------------------------------------------------------------------


def test_default_knowledge_por_stack(monkeypatch):
    _set(monkeypatch)
    assert "professional/go_best_practices.md" in stacks.default_knowledge("development_agent")
    assert "professional/pci_checklist.md" in stacks.default_knowledge("pr_review_agent")
    _set(monkeypatch, STACK="python")
    dev = stacks.default_knowledge("development_agent")
    assert "professional/go_best_practices.md" not in dev and "professional/pci_checklist.md" not in dev
    assert stacks.default_knowledge("architecture_agent") == [
        "professional/hexagonal_architecture.md", "professional/design_patterns.md",
    ]
    _set(monkeypatch, STACK="python", COMPLIANCE=["pci"])
    assert "professional/pci_checklist.md" in stacks.default_knowledge("pr_review_agent")


def test_loader_respeta_lista_explicita_y_usa_defaults_si_falta(monkeypatch):
    _set(monkeypatch, STACK="python", AGENT_KNOWLEDGE={"debug_agent": []})
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    assert len(KnowledgeLoader.load_for_agent("debug_agent")) == 0  # [] explícito manda
    arch = KnowledgeLoader.load_for_agent("architecture_agent")
    assert set(arch.docs) == {"professional/hexagonal_architecture.md", "professional/design_patterns.md"}
    assert "go_best_practices" not in arch.get_relevant_context()
    _set(monkeypatch, AGENT_KNOWLEDGE={})
    dev = KnowledgeLoader.load_for_agent("development_agent")
    assert "professional/go_best_practices.md" in dev.docs and "professional/pci_checklist.md" in dev.docs


# ---------------------------------------------------------------------------
# riesgos por stack
# ---------------------------------------------------------------------------


def test_detect_risks_reglas_por_stack_y_risk_patterns(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "m.py").write_text(
        "import subprocess\ntry:\n    eval(x)\nexcept:\n    pass\nsubprocess.run(cmd, shell=True)\n", encoding="utf-8"
    )
    (tmp_path / "app" / "m.go").write_text('func a() { panic("x") }\n', encoding="utf-8")
    _set(monkeypatch, STACK="python")
    out = risk_analyzer.detect_risks(str(tmp_path))
    assert "EVAL_EXEC" in out and "BARE_EXCEPT" in out and "SHELL_TRUE" in out
    assert "PANIC_PROD" not in out and "m.go" not in out
    _set(monkeypatch, STACK="python", RISK_PATTERNS=[("EMPRESA_X", "ALTA", r"\bcmd\b", "regla del proyecto")])
    assert "EMPRESA_X" in risk_analyzer.detect_risks(str(tmp_path))
    _set(monkeypatch)
    out_go = risk_analyzer.detect_risks(str(tmp_path))
    assert "PANIC_PROD" in out_go and "EVAL_EXEC" not in out_go and "m.py" not in out_go
    _set(monkeypatch, STACK="node")
    (tmp_path / "app" / "n.js").write_text("const f = new Function('a'); eval(a);\n", encoding="utf-8")
    out_js = risk_analyzer.detect_risks(str(tmp_path))
    assert "NEW_FUNCTION" in out_js and "EVAL" in out_js and "m.py" not in out_js


# ---------------------------------------------------------------------------
# prueba de aceptación: repo Python temporal
# ---------------------------------------------------------------------------


class _FakeCollection:
    def __init__(self):
        self.docs: dict[str, dict] = {}

    def get(self, where=None, include=None):
        metas = [d["metadatas"] for d in self.docs.values() if all(d["metadatas"].get(k) == v for k, v in (where or {}).items())]
        return {"metadatas": metas}

    def delete(self, where=None):
        self.docs = {k: d for k, d in self.docs.items() if d["metadatas"].get("file") != where.get("file")}

    def upsert(self, ids, documents, metadatas, **kw):
        for i, doc, meta in zip(ids, documents, metadatas):
            self.docs[i] = {"document": doc, "metadatas": meta}


class _FakeClient:
    def __init__(self):
        self.collection = _FakeCollection()

    def get_or_create_collection(self, name, **kw):
        return self.collection


@pytest.fixture
def py_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "svc-py"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "main.py").write_text(
        '"""demo"""\nimport os\n\ndef resolve_token():\n    return os.getenv("TOKEN", "")\n\nclass Charger:\n    def charge(self, amount):\n        return amount > 0\n',
        encoding="utf-8",
    )
    (repo / ".venv" / "lib").mkdir(parents=True)
    (repo / ".venv" / "lib" / "x.py").write_text("ignorado\n", encoding="utf-8")
    (repo / "README.md").write_text("# demo\n", encoding="utf-8")
    return repo


def test_aceptacion_repo_python(py_repo, monkeypatch):
    _set(monkeypatch, STACK="python", REPOS={"app": str(py_repo)}, RAG_ENABLED=True)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    # grep_code / list_files / scan_directory ven .py por defecto
    assert "main.py" in read_tools.list_files(str(py_repo))
    assert "x.py" not in read_tools.list_files(str(py_repo))  # .venv excluido por el glob+skip del stack
    assert "main.py" in read_tools.scan_directory(str(py_repo), max_depth=3)
    hits = search_tools.grep_code("resolve_token", str(py_repo))
    assert "main.py" in hits and "ERROR" not in hits
    # --index-repos indexa >0 archivos con chunks por función/clase
    client = _FakeClient()
    monkeypatch.setattr(indexer, "get_client", lambda: client)
    cfg = SimpleNamespace(RAG_ENABLED=True, REPOS={"app": str(py_repo)})
    stats = indexer.index_repos(cfg, only_changed=False)
    assert stats["files"] == 1 and stats["chunks"] >= 2 and stats["errors"] == 0
    names = {d["metadatas"].get("name") for d in client.collection.docs.values()}
    assert {"resolve_token", "Charger"} <= names
    # El agente de architecture no recibe knowledge de Go ni tools de Go
    kb = KnowledgeLoader.load_for_agent("architecture_agent").get_relevant_context()
    assert "go_best_practices" not in kb and "Go" not in kb.split("Documento:")[0]
    tools = [t["name"] for t in ToolRegistry.get_tools_for_agent("architecture_agent")]
    assert not (set(tools) & stacks.GO_ONLY_TOOLS) and ".go" not in json.dumps(ToolRegistry.get_tools_for_agent("architecture_agent"))


def test_aceptacion_repo_go_sigue_igual(tmp_path, monkeypatch):
    repo = tmp_path / "svc-go"
    (repo / "service").mkdir(parents=True)
    (repo / "service" / "a.go").write_text("package service\n\nfunc Charge() {}\n\ntype ICharger interface {}\n", encoding="utf-8")
    (repo / "vendor" / "x").mkdir(parents=True)
    (repo / "vendor" / "x" / "v.go").write_text("package x\n", encoding="utf-8")
    _set(monkeypatch, REPOS={"pos": str(repo)}, RAG_ENABLED=True)
    assert "a.go" in read_tools.list_files(str(repo)) and "v.go" not in read_tools.list_files(str(repo))
    assert "a.go" in search_tools.grep_code("Charge", str(repo))
    client = _FakeClient()
    monkeypatch.setattr(indexer, "get_client", lambda: client)
    stats = indexer.index_repos(SimpleNamespace(RAG_ENABLED=True, REPOS={"pos": str(repo)}), only_changed=False)
    assert stats["files"] == 1 and stats["chunks"] == 2
    assert {d["metadatas"].get("type") for d in client.collection.docs.values()} == {"func", "interface"}
    tools = [t["name"] for t in ToolRegistry.get_tools_for_agent("architecture_agent")]
    assert set(stacks.GO_ONLY_TOOLS) <= set(tools)
