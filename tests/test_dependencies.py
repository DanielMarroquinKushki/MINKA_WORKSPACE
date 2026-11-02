"""Todo import de terceros en src/minka debe estar declarado en pyproject.toml.

Base: imports a nivel de módulo. Extras: solo dentro de una función o de un try
(si falta el extra, el motor arranca igual).
"""

import ast
import re
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "minka"
# Nombre de import → distribución, cuando difieren y el paquete no está instalado.
_IMPORT_TO_DIST = {"dotenv": "python-dotenv", "mem0": "mem0ai", "git": "gitpython"}


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _req_name(req: str) -> str:
    return _norm(re.split(r"[<>=!~\[; ]", req, maxsplit=1)[0])


def _declared(pyproject: dict) -> tuple[set, set]:
    project = pyproject["project"]
    base = {_req_name(r) for r in project["dependencies"]}
    extras = {_req_name(r) for reqs in project["optional-dependencies"].values() for r in reqs}
    return base, extras


def _third_party_imports() -> list[tuple[str, Path, int, bool]]:
    """(módulo, archivo, línea, opcional) — opcional = dentro de función o try."""
    stdlib = set(sys.stdlib_module_names) | {"__future__"}
    found = []

    def walk(node, optional, path):
        for child in ast.iter_child_nodes(node):
            opt = optional or isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Try))
            mods = []
            if isinstance(child, ast.Import):
                mods = [a.name for a in child.names]
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                mods = [child.module]
            for mod in mods:
                top = mod.split(".")[0]
                if top not in stdlib and top != "minka":
                    found.append((top, path, child.lineno, opt))
            walk(child, opt, path)

    for f in sorted(SRC.rglob("*.py")):
        walk(ast.parse(f.read_text(encoding="utf-8")), False, f.relative_to(ROOT))
    return found


def undeclared_imports(pyproject: dict) -> list[str]:
    base, extras = _declared(pyproject)
    installed = packages_distributions()
    problems = []
    for top, path, line, optional in _third_party_imports():
        dists = {_norm(d) for d in installed.get(top, [])} or {
            _norm(_IMPORT_TO_DIST.get(top, top))
        }
        allowed = base | extras if optional else base
        if not dists & allowed:
            kind = "extra/base" if optional else "base"
            problems.append(f"{path}:{line} importa '{top}' sin dependencia {kind} declarada")
    return problems


def test_imports_de_terceros_declarados():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert undeclared_imports(pyproject) == []


def test_extra_rag_no_trae_torch():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]
    rag = {_req_name(r) for r in extras["rag"]}
    assert rag == {"chromadb"}
    assert {_req_name(r) for r in extras["rerank"]} == {"sentence-transformers"}
    assert {"chromadb", "mem0ai"} <= {_req_name(r) for r in extras["memory"]}
