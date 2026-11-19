"""Paso 12b-C — búsqueda fuera del stack (A) y nota "Búsqueda sobre" con archivos recorridos (B).
Sin red: repo git temporal, agentes falsos. La clave AWS de ejemplo se construye en runtime (nunca literal)."""

from __future__ import annotations

import subprocess

import pytest

from minka.config_loader import config
from minka.core import baseline, security
from minka.core.orchestrator import absence_check as ac
from minka.core.orchestrator.orchestrator import Orchestrator
from minka.core.tools.filesystem import search_tools
from minka.core.tools.git import branches as repo_branches

FAKE_AWS_KEY = "AKIA" + "J" * 16
NAME = "triggerKeyExchange"
MANY = "RepeatedName_x"


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "svc-hsm"
    (r / "vendor").mkdir(parents=True)
    (r / "scripts").mkdir()
    (r / "a.go").write_text("package a\n\nfunc Alpha() {}\n")
    (r / "serverless.yml").write_text(f"service: svc\nfunctions:\n  {NAME}:\n    handler: bin/{NAME}\n")  # línea 3 y 4
    (r / ".env").write_text(f"SECRET={NAME}\n")
    (r / "secrets.yaml").write_text(f"{NAME}: s3cr3t\n")
    (r / "credentials").write_text(f"[default]\n{NAME}=x\n")
    (r / "server.pem").write_text(f"-----BEGIN-----{NAME}-----END-----\n")
    (r / "package-lock.json").write_text('{"name": "' + NAME + '"}\n')
    (r / "go.sum").write_text(f"{NAME} h1:abc\n")
    (r / "vendor" / "x.yml").write_text(f"{NAME}: vendor\n")
    (r / "config.yml").write_text(f"{NAME}: {FAKE_AWS_KEY} " + "x" * 200 + "\n")
    (r / "Makefile").write_text(f"deploy:\n\tsls deploy --function {NAME}\n")
    for i in range(10):  # 10 scripts × 5 líneas con MANY: prueba los topes 3 por archivo / 20 en total
        (r / "scripts" / f"s{i}.sh").write_text("".join(f"echo {MANY} {j}\n" for j in range(5)))
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    monkeypatch.setattr(config, "STACK", "go", raising=False)
    for key in ("EXTRA_SEARCH_GLOBS", "EXTRA_SEARCH_SKIP"):
        monkeypatch.setattr(config, key, getattr(config, key), raising=False)
    monkeypatch.setattr(config, "SECRET_REDACTION_ENABLED", True, raising=False)
    return r


@pytest.fixture(params=[True, False], ids=["rg", "python"])
def rg(request, monkeypatch):
    if request.param and not search_tools._HAS_RG:
        pytest.skip("ripgrep no instalado en esta máquina")
    monkeypatch.setattr(search_tools, "_HAS_RG", request.param)
    return request.param


# ---------------------------------------------------------------------------
# A. pasada fuera del stack
# ---------------------------------------------------------------------------


def test_literal_sin_hits_en_go_encuentra_serverless(repo, rg):
    out = search_tools.grep_code(NAME, str(repo))
    lines = out.splitlines()
    assert lines[0] == "[sin coincidencias en *.go]" and lines[1].startswith("[fuera del stack: ") and "coincidencias]" in lines[1]
    assert f"serverless.yml:3: {NAME}:" in out and "serverless.yml:4:" in out and "Makefile:2:" in out
    # find_usages pasa el nombre literal aunque su regex lleve \b
    assert "serverless.yml:3:" in search_tools.find_usages(NAME, str(repo))
    # con coincidencias en el stack no hay segunda pasada
    assert "fuera del stack" not in search_tools.grep_code("Alpha", str(repo))


def test_regex_no_dispara_la_segunda_pasada(repo, rg):
    assert search_tools.grep_code(rf"{NAME}\w*", str(repo)) == search_tools.NO_HITS
    assert search_tools.grep_code(f"{NAME}|otro", str(repo)) == search_tools.NO_HITS
    # un glob elegido por el agente se respeta tal cual
    assert search_tools.grep_code(NAME, str(repo), glob="*.md") == search_tools.NO_HITS
    assert search_tools.is_literal(NAME) and not search_tools.is_literal(r"\bX\b")


def test_sensibles_y_vendor_nunca_salen(repo, rg):
    out = search_tools.grep_code(NAME, str(repo))
    for rel in (".env", "secrets.yaml", "credentials", "server.pem", "vendor/x.yml"):
        assert rel not in out, rel
    assert "s3cr3t" not in out
    extra = search_tools.extra_search(NAME, repo)
    assert extra and all(("secrets" not in h and ".env" not in h and "vendor/" not in h and ".pem" not in h) for h in extra)
    # una exclusión del proyecto también poda
    config.EXCLUDE_PATTERNS_EXTRA = ("Makefile",)
    try:
        assert "Makefile" not in search_tools.grep_code(NAME, str(repo))
    finally:
        config.EXCLUDE_PATTERNS_EXTRA = ()


def test_lockfiles_y_skip_saltados(repo):
    out = search_tools.grep_code(NAME, str(repo))
    assert "package-lock.json" not in out and "go.sum" not in out
    # *.json no está por defecto; el proyecto puede añadirlo y aun así el lockfile queda fuera
    config.EXTRA_SEARCH_GLOBS = (*config.EXTRA_SEARCH_GLOBS, "*.json")
    (repo / "config.json").write_text('{"fn": "' + NAME + '"}\n')
    out = search_tools.grep_code(NAME, str(repo))
    assert "config.json:1:" in out and "package-lock.json" not in out


def test_globs_vacios_apagan_la_pasada(repo, rg):
    config.EXTRA_SEARCH_GLOBS = ()
    assert search_tools.grep_code(NAME, str(repo)) == search_tools.NO_HITS
    assert search_tools.extra_search(NAME, repo) == []


def test_linea_con_clave_falsa_sale_redactada_y_truncada(repo):
    out = search_tools.grep_code(NAME, str(repo))
    line = next(ln for ln in out.splitlines() if "config.yml:1:" in ln)
    assert FAKE_AWS_KEY not in out and "[REDACTED:AWS_ACCESS_KEY]" in line
    body = line.split(": ", 1)[1]
    assert body.endswith("…") and len(body) <= 160 + len("…") + len("[REDACTED:AWS_ACCESS_KEY]")


def test_tope_de_3_por_archivo_y_20_en_total(repo):
    hits = search_tools.extra_search(MANY, repo)
    assert len(hits) == 20
    per_file: dict[str, int] = {}
    for h in hits:
        per_file[h.split(":")[0]] = per_file.get(h.split(":")[0], 0) + 1
    assert max(per_file.values()) == 3 and len(per_file) == 7  # 6 archivos × 3 + 2 del séptimo
    assert search_tools.grep_code(MANY, str(repo)).splitlines()[1] == "[fuera del stack: 20 coincidencias]"
    big = repo / "scripts" / "big.sh"
    big.write_text(f"echo {MANY}\n" + "#" * 1_100_000)
    assert not any("big.sh" in h for h in search_tools.extra_search(MANY, repo))  # > 1 MB: se salta


# ---------------------------------------------------------------------------
# B. nota "Búsqueda sobre" con archivos recorridos y aviso de infraestructura
# ---------------------------------------------------------------------------


class _FakeAgent:
    def __init__(self, name, reply, tools):
        self.name, self.reply = name, reply
        self.last_usage = {"agent": name, "model": "claude-haiku-4-5", "requests": 1, "input_tokens": 10,
                           "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                           "cache_creation_5m": 0, "cache_creation_1h": 0,
                           "per_iteration": [{"tools": [{"name": n, "args": a} for n, a in tools]}]}

    def run(self, task):
        return self.reply


def _orch(monkeypatch, repo, reply, tools):
    for key, value in (("USE_BEDROCK", False), ("EVALUATOR_ENABLED", False), ("META_AGENT_ENABLED", False),
                       ("REPOS", {"hsm": str(repo)}), ("SESSION_MAX_USD", 100.0), ("BURST_GAP_SECONDS", 0)):
        monkeypatch.setattr(config, key, value, raising=False)
    o = Orchestrator()
    o.quiet = True
    o.route_medians_fn = lambda: {}
    monkeypatch.setattr(o, "_get_agent", lambda name: _FakeAgent(name, reply, tools))
    return o


INFRA_REPLY = f"No hay `{NAME}` en el hsm: la lambda no está definida."
PLAIN_REPLY = "No existe `OtraCosa` en el código del hsm."


def test_nota_sin_yaml_recorrido_avisa_y_con_yaml_no(repo, monkeypatch):
    config.EXTRA_SEARCH_GLOBS = ()  # sin pasada extra: solo cuentan los globs de las tools
    shown = _orch(monkeypatch, repo, INFRA_REPLY, [("grep_code", {"pattern": NAME, "path": str(repo), "glob": "*.go"})]).run("¿dónde está definida la lambda en el repo hsm?")
    assert "\n_Búsqueda sobre: archivos: *.go · no se buscó en YAML/config_\n" in shown
    shown = _orch(monkeypatch, repo, INFRA_REPLY, [("grep_code", {"pattern": NAME, "path": str(repo), "glob": "*.yml"}),
                                                   ("find_usages", {"symbol": NAME, "path": str(repo)})]).run("¿dónde está definida la lambda en el repo hsm?")
    assert "Búsqueda sobre" not in shown  # recorrió YAML y está en su base: sin ruido
    # negación sin pinta de infraestructura: tampoco hay nota (comportamiento previo)
    shown = _orch(monkeypatch, repo, PLAIN_REPLY, [("grep_code", {"pattern": "OtraCosa", "path": str(repo)})]).run("¿dónde se define OtraCosa en el repo hsm?")
    assert "Búsqueda sobre" not in shown
    # con la pasada extra activa, un grep literal cuenta como haber recorrido sus globs
    config.EXTRA_SEARCH_GLOBS = ("serverless.yml", "*.yml")
    o = _orch(monkeypatch, repo, INFRA_REPLY, [("grep_code", {"pattern": NAME, "path": str(repo)})])
    shown = o.run("¿dónde está definida la lambda en el repo hsm?")
    assert o.searched_globs() == ["*.go", "serverless.yml", "*.yml"] and "Búsqueda sobre" not in shown
    # la nota no entra en la memoria
    assert "Búsqueda sobre" not in o.memory.turns[-1].result


def test_nota_fuera_de_base_lista_ramas_y_archivos(repo, monkeypatch):
    _git(repo, "checkout", "-q", "-b", "feature/x")
    (repo / "b.go").write_text("package a\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")
    config.EXTRA_SEARCH_GLOBS = ()
    shown = _orch(monkeypatch, repo, PLAIN_REPLY, [("grep_code", {"pattern": "OtraCosa", "path": str(repo)}),
                                                   ("list_files", {"path": str(repo), "pattern": "*.md"})]).run("¿dónde se define OtraCosa en el repo hsm?")
    assert "\n_Búsqueda sobre: hsm@feature/x (+1 sobre main) · archivos: *.go, *.md_\n" in shown
    shown = _orch(monkeypatch, repo, INFRA_REPLY, [("scan_directory", {"path": str(repo), "include_ext": None})]).run("¿dónde está definida la lambda en el repo hsm?")
    assert "_Búsqueda sobre: hsm@feature/x (+1 sobre main) · archivos: *.go · no se buscó en YAML/config_" in shown
    rb = repo_branches.RepoBranches({"hsm": str(repo)})
    assert rb.search_note() == "Búsqueda sobre: hsm@feature/x (+1 sobre main)"
    assert rb.search_note(files="*.go", warning="w") == "Búsqueda sobre: hsm@feature/x (+1 sobre main) · archivos: *.go · w"


def test_infra_absences_y_config_globs():
    assert ac.infra_absences(INFRA_REPLY) == [NAME] and ac.infra_absences(PLAIN_REPLY) == []
    assert ac.infra_absences("en dynamo no existe la `Pagos_x` (tabla).\n```\nno hay `cola` lambda\n```") == ["Pagos_x"]
    assert all(ac.is_config_glob(g) for g in ("*.yml", "serverless.yml", "*.yaml", "Makefile", "*.json", "*.sh", "*", "**/*"))
    assert not any(ac.is_config_glob(g) for g in ("*.go", "**/*.go", "gateway_sender.go", None, ""))


# ---------------------------------------------------------------------------
# prompt_hash intacto
# ---------------------------------------------------------------------------


def test_prompt_hash_del_motor_sigue_en_la_linea_base(monkeypatch):
    from pathlib import Path

    monkeypatch.setattr(config, "ACTIVE_PROJECT", None, raising=False)
    baseline.reset_cache()
    expected = Path(__file__).resolve().parents[1].joinpath("baseline", "prompt_hash_motor.txt").read_text().strip()
    assert baseline.compute_prompt_hash() == expected
    assert security.scan_text(Path(__file__).read_text(encoding="utf-8"), __file__) == [] or all(
        h["level"] != "fallo" for h in security.scan_text(Path(__file__).read_text(encoding="utf-8"), __file__)
    )
