"""Lote 3: verificador (nombres de tools, inferidas, runtime, inconsistencia entre corridas),
selector por prefijo, lista cerrada de valores de prueba y marcas de ejemplo en el escaneo. Sin red."""

from __future__ import annotations

import copy
from types import SimpleNamespace

from minka.config_loader import config
from minka.core import metrics, security
from minka.core.agents import base_agent
from minka.core.agents.base_agent import BaseAgent
from minka.core.memory import decisions as dec
from minka.core.memory.session_memory import SessionMemory
from minka.core.orchestrator import claim_check as cc
from minka.core.orchestrator import response_profile as rp
from minka.core.tools.analysis.citations import RepoIndex
from minka.core.tools.registry import ToolRegistry

TOOLS = frozenset(ToolRegistry.all_names())
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"


# ---------------------------------------------------------------------------
# 1. verificador
# ---------------------------------------------------------------------------


def test_nombre_de_herramienta_no_se_marca_sin_respaldo():
    r = cc.verify("Lo vi con `grep_code` y `read_file()`; nada más.", RepoIndex({}), previous=[], tool_names=TOOLS)
    assert cc.MARK_UNSUPPORTED not in r.text and r.identifiers_checked == 0 and r.tools_named == 2
    # Sin la lista de tools, el nombre snake_case se trataría como identificador del proyecto.
    r2 = cc.verify("Lo vi con `grep_code`.", RepoIndex({}), previous=[])
    assert cc.MARK_UNSUPPORTED in r2.text


def test_herramienta_inferida_sin_ejecutar():
    r = cc.verify("Busqué con `grep_code` y leí con `read_file`.", RepoIndex({}), previous=[],
                  tool_names=TOOLS, tools_used={"read_file"})
    assert f"{cc.MARK_INFERRED}: grep_code)" in r.text and r.claims_inferred == 1 and r.tools_named == 2
    # Si se ejecutó, no se marca; si no sabemos qué se ejecutó (None), tampoco.
    assert cc.MARK_INFERRED not in cc.verify("Leí con `read_file`.", RepoIndex({}), [], tool_names=TOOLS, tools_used={"read_file"}).text
    assert cc.MARK_INFERRED not in cc.verify("Leí con `read_file`.", RepoIndex({}), [], tool_names=TOOLS, tools_used=None).text


def test_cadena_de_runtime_no_es_hallazgo():
    text = "La clave se arma como `%s|%s` con processor y country (`fmt.Sprintf(\"%s|%s\", p, c)`)."
    r = cc.verify(text, RepoIndex({}), previous=[])
    assert cc.MARK_RUNTIME in r.text and cc.MARK_UNSUPPORTED not in r.text
    assert r.runtime_strings == 2 and r.identifiers_checked == 0


def test_inconsistencia_entre_corridas_y_sin_falso_positivo():
    mem = cc.ConsistencyMemory()
    q = "¿Dónde se RESUELVE el token?"
    first = "El token se resuelve en `ResolveToken()` con timeout 30 segundos (pos/token.go:12)."
    r1 = cc.verify(first, RepoIndex({}), previous=[first], consistency=mem, question=q)
    assert r1.claims_inconsistent == 0 and cc.MARK_INCONSISTENT not in r1.text
    # Misma pregunta (normalizada distinto), misma cita y mismos valores: sin aviso.
    r2 = cc.verify(first, RepoIndex({}), previous=[first], consistency=mem, question="¿donde se resuelve el token?")
    assert r2.claims_inconsistent == 0 and cc.MARK_INCONSISTENT not in r2.text
    # Misma cita con otros valores: aviso y contador.
    other = "El token se resuelve en `FetchToken()` con timeout 45 segundos (pos/token.go:12)."
    r3 = cc.verify(other, RepoIndex({}), previous=[other], consistency=mem, question=q)
    assert r3.claims_inconsistent == 1 and r3.text.rstrip().endswith(cc.MARK_INCONSISTENT)
    # Otra pregunta no se compara con esta.
    r4 = cc.verify(other, RepoIndex({}), previous=[other], consistency=mem, question="¿cómo se firma el request?")
    assert r4.claims_inconsistent == 0
    mem.clear()
    assert cc.verify(first, RepoIndex({}), previous=[first], consistency=mem, question=q).claims_inconsistent == 0


def test_citas_intactas_con_marcas_nuevas():
    text = "Ver `grep_code` y `%s-%s` en pos/a.go:3 y `Foo_bar()` en pos/b.go:7."
    r = cc.verify(text, RepoIndex({}), previous=[], tool_names=TOOLS, tools_used=set())
    assert "pos/a.go:3" in r.text and "pos/b.go:7" in r.text


def test_metricas_inferidas_e_inconsistentes():
    usage = {"per_agent": [{"agent": "architecture_agent", "model": "claude-haiku-4-5", "requests": 1}], "totals": {}}
    rec = metrics.build_run_record(run_id="r", source="cli", project="p", cwd="~", router="llm", plan=[],
                                   fix_loop_iters=0, ok=True, duration_s=1.0, usage=usage,
                                   claims={"inferred": 2, "inconsistent": 1})
    assert (rec["claims_inferred"], rec["claims_inconsistent"]) == (2, 1)
    agg = metrics.aggregate([rec, rec])
    assert (agg["claims_inferred"], agg["claims_inconsistent"]) == (4, 2)


# ---------------------------------------------------------------------------
# 2. selector por prefijo
# ---------------------------------------------------------------------------

LOG = """## ADR-010 — Anulación con el endpoint nuevo
**Fecha:** 2026-10-01
**Estado:** aceptada
**Contexto:** cancelar compras del día.
**Decisión:** la anulación usa el endpoint nuevo.

## ADR-011 — Rotar la working key al arrancar
**Fecha:** 2026-10-02
**Estado:** aceptada
**Contexto:** la WK vence.
**Decisión:** rotar la WK en el arranque.

## ADR-012 — Procesador de reintentos
**Fecha:** 2026-10-03
**Estado:** aceptada
**Contexto:** procesar colas.
**Decisión:** el procesador reintenta tres veces.
"""


def test_prefijo_casa_anula_anular_anulacion():
    ds = dec.parse_decisions(LOG)
    assert dec.select(ds, "¿cómo anular una compra?", prefix=False) == []  # "anular" ≠ "anulacion"
    pref = dec.select(ds, "¿cómo anular una compra?", prefix=True, prefix_exclude=())
    assert [d.id for d in pref] == ["ADR-010"]  # anula|anular|anulacion comparten "anula"


def test_prefijo_no_casa_rotacion_con_rotar():
    ds = dec.parse_decisions(LOG)
    # "rotacion"[:5] = "rotac" ≠ "rotar"[:5] = "rotar": documentado, el prefijo de 5 letras no lo cubre.
    assert dec.select(ds, "estado de la rotación", prefix=True, prefix_exclude=()) == []
    assert dec.select(ds, "estado de la rotación de la WK", prefix=True, prefix_exclude=())[0].id == "ADR-011"  # por la sigla


def test_prefijo_solo_con_menos_de_dos_exactas_y_excepciones():
    ds = dec.parse_decisions(LOG)
    # Con 2+ exactas el prefijo no entra aunque esté activo.
    ex = dec.select(ds, "anulación y working key", prefix=False)
    pr = dec.select(ds, "anulación y working key", prefix=True, prefix_exclude=())
    assert [d.id for d in ex] == [d.id for d in pr] and len(ex) == 2
    # "proce" en la lista de excepciones: "proceso" no trae al procesador.
    assert dec.select(ds, "¿qué proceso corre?", prefix=True, prefix_exclude=()) and \
        dec.select(ds, "¿qué proceso corre?", prefix=True, prefix_exclude=("proce",)) == []


def test_prefijo_apagado_por_defecto_en_config():
    assert config.CONTEXT_DECISIONS_PREFIX is False and "proce" in config.CONTEXT_DECISIONS_PREFIX_EXCLUDE


def test_puntuacion_exacta_dos_titulo_cuatro():
    ds = dec.parse_decisions(LOG)
    terms = dec._terms("anulación endpoint")
    assert dec._score(ds[0], terms, None, ()) == 8  # 2 términos exactos, ambos en el título: (2+2)·2
    assert dec._score(ds[1], terms, None, ()) == 0


# ---------------------------------------------------------------------------
# 3. knowledge scan: lista cerrada y marcas de ejemplo
# ---------------------------------------------------------------------------


def test_lista_cerrada_de_valores_de_prueba():
    assert security.TEST_VALUES == ("0123456789ABCDEF", "FEDCBA9876543210")
    text = (f"clave real {AWS_KEY}\n"
            "KEK de prueba " + "0123456789ABCDEF" * 3 + "\n"
            "otra " + "FEDCBA9876543210" * 3 + "\n"
            "relleno AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA\n"
            f"hex real {'c3d4e5f6' * 5}\n")
    hits = security.scan_text(text, "doc.md")
    by_line = {h["line"]: h for h in hits}
    assert by_line[1]["level"] == "fallo"
    assert by_line[2]["level"] == "ejemplo" and by_line[2]["example"] == "lista"
    assert by_line[3]["level"] == "ejemplo" and by_line[4]["level"] == "ejemplo"
    assert by_line[5]["level"] == "revisar"
    s = security.scan_summary(hits)
    assert (s["fails"], s["review"], s["example_list"], s["example_mark"]) == (1, 1, 3, 0)
    assert "3 ejemplo(s) (0 por marca, 3 por lista)" in security.format_scan(hits)


def test_marca_de_linea_y_de_bloque():
    text = (f"token {AWS_KEY} <!-- ejemplo -->\n"
            "<!-- ejemplo:inicio -->\n"
            f"otro {AWS_KEY}\n"
            f"hash {'ab' * 20}\n"
            "<!-- ejemplo:fin -->\n"
            f"fuera {AWS_KEY}\n")
    hits = security.scan_text(text, "doc.md")
    levels = {h["line"]: (h["level"], h["example"]) for h in hits}
    assert levels[1] == ("ejemplo", "marca") and levels[3] == ("ejemplo", "marca") and levels[4] == ("ejemplo", "marca")
    assert levels[6] == ("fallo", "")
    s = security.scan_summary(hits)
    assert (s["fails"], s["example_mark"], s["example_list"]) == (1, 3, 0)


def test_la_marca_no_desactiva_la_redaccion(tmp_path, monkeypatch):
    from minka.core.tools.filesystem import read_tools

    repo = tmp_path / "pos"
    repo.mkdir()
    (repo / "notas.go").write_text(f"// clave {AWS_KEY} <!-- ejemplo -->\n// prueba 0123456789ABCDEF\n")
    monkeypatch.setattr(config, "REPOS", {"pos": str(repo)}, raising=False)
    monkeypatch.setattr(config, "SECRET_REDACTION_ENABLED", True, raising=False)
    raw = read_tools.read_file(str(repo / "notas.go"))
    assert AWS_KEY in raw  # la tool devuelve el archivo…
    redacted, counts = security.redact(raw)
    assert AWS_KEY not in redacted and counts["AWS_ACCESS_KEY"] == 1  # …y la redacción lo tapa igual
    assert "0123456789ABCDEF" in redacted  # el valor de prueba no es un secreto para redact


def test_scan_cli_y_doctor_cuentan_ejemplos(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from minka import main as minka_main
    from minka.core import doctor

    kn = tmp_path / "projects" / "demo" / "knowledge" / "extracted"
    kn.mkdir(parents=True)
    (kn / "a.md").write_text("KEK " + "0123456789ABCDEF" * 3 + f"\nreal {AWS_KEY} <!-- ejemplo -->\n")
    monkeypatch.setattr(config, "PROJECTS_DIR", tmp_path / "projects", raising=False)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "demo", raising=False)
    monkeypatch.setattr(config, "RISK_PATTERNS", [], raising=False)
    result = CliRunner().invoke(minka_main.app, ["knowledge", "scan"])
    assert result.exit_code == 0, result.output
    assert "[ejemplo por lista]" in result.output and "[ejemplo por marca]" in result.output
    assert "0 fallo(s), 0 a revisar, 2 ejemplo(s) (1 por marca, 1 por lista)" in result.output
    assert AWS_KEY not in result.output
    (tmp_path / "data").mkdir()
    cfg = SimpleNamespace(DATA_DIR=tmp_path / "data", PROJECTS_DIR=tmp_path / "projects", ACTIVE_CONTEXT_DIR=None,
                          ACTIVE_PROJECT="demo", USE_BEDROCK=False, REPOS={"pos": str(tmp_path)}, RAG_ENABLED=False)
    sec = next(c for c in doctor.run_checks(cfg) if c.name == "Secretos en knowledge")
    assert sec.ok and "2 ejemplo(s) (1 por marca, 1 por lista)" in sec.detail


# ---------------------------------------------------------------------------
# 5. system y tools byte-idénticos: el verificador corre después, fuera del mensaje
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
        return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="ok `grep_code`")],
                               usage=SimpleNamespace(input_tokens=1, output_tokens=1, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0, cache_creation=None), model="m")


def test_system_y_tools_identicos_con_verificador_activo(monkeypatch):
    def make():
        a = _Agent.__new__(_Agent)
        a.name, a.config, a.client = "architecture_agent", config, _Client()
        a.memory = SessionMemory(agent_name=a.name)
        a.vector_memory = SimpleNamespace(search=lambda *x, **k: [], add=lambda *x, **k: None)
        a.tools = [{"name": "read_file", "description": "d", "input_schema": {"type": "object"}}]
        a.knowledge = SimpleNamespace(get_relevant_context=lambda: "KB")
        a.iteration_count, a.max_iterations = 0, 5
        a.last_usage = a._empty_usage()
        a.live_context_block, a.decisions_block, a.voice, a.response_profile = "", "", None, None
        monkeypatch.setattr(a, "_log", lambda *x, **k: None)
        return a

    monkeypatch.setattr(config, "PROMPT_CACHE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    monkeypatch.setattr(base_agent, "trace", lambda *a, **k: _noop_ctx())
    monkeypatch.setattr(base_agent.progress, "track", lambda name: _noop_ctx())
    monkeypatch.setattr(base_agent.console, "print", lambda *a, **k: None)
    a, b = make(), make()
    out_a = a.run("¿dónde está X?")
    out_b = b.run("¿dónde está X?")
    mem = cc.ConsistencyMemory()
    cc.verify(out_a, RepoIndex({}), [], tool_names=TOOLS, tools_used=set(), consistency=mem, question="q")
    cc.verify(out_b, RepoIndex({}), [], tool_names=TOOLS, tools_used=set(), consistency=mem, question="q")
    assert a.client.requests[0]["system"] == b.client.requests[0]["system"]
    assert a.client.requests[0]["tools"] == b.client.requests[0]["tools"]
    assert rp.user_block(rp.PROFILE_BRIEF) == rp.user_block(rp.PROFILE_BRIEF)
