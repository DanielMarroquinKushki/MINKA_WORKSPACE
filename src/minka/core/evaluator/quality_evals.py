"""quality_evals — `minka evals quality`: golden set por proyecto con verificación determinista.

Cada caso (`regression_evals.EvalCase`) tiene un `type`:
  existence    ¿existe X en el código? → la afirmación de la respuesta se compara con `git grep`.
  location     ¿dónde está X? → alguna cita archivo:línea apunta al archivo esperado y el símbolo
               está a ≤ SYMBOL_WINDOW líneas (core/tools/analysis/citations.py).
  count        ¿cuántos X hay? → el número de la respuesta vs el conteo real de `git grep`.
  absence      debe decir que no encontró ("[NO ENCONTRADO EN CÓDIGO]", "no hay decisión registrada"…).
  consistency  n corridas: mismo conjunto de archivos citados.
  legacy       los casos antiguos: `keyword_coverage` sobre `must_mention`.
Sin juez LLM. Si la afirmación no se puede interpretar con confianza, el caso es "no evaluable"
(no cuenta como acierto ni como fallo). `repo_commit` (location y consistency) se valida a $0 contra
el código vivo antes de gastar: si el esperado ya no se sostiene, el caso queda "caducado".
Cada corrida va en un `Orchestrator` nuevo (sin historial), por el router salvo `forced_agent`,
con MINKA_SOURCE=eval_quality (excluida de `minka stats` por defecto). El tope de gasto se
proyecta ANTES de cada caso con la mediana real de su ruta y se corta al superarlo.
Los casos reales viven en `<DATA_DIR>/projects/<p>/evals/cases.json`; en el repo solo la
plantilla genérica `core/evals/quality_cases.json` y los tests.
"""

from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from minka._app import SOURCE_ENV
from minka.core import metrics
from minka.core.evaluator.regression_evals import EvalCase, keyword_coverage, load_golden_set
from minka.core.tools.analysis import citations as cit
from minka.core.tools.analysis.freshness import changed_files, git_head

SOURCE = "eval_quality"
TYPES = ("legacy", "existence", "location", "count", "absence", "consistency")
COMMIT_TYPES = ("location", "consistency")
TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "evals" / "quality_cases.json"
DEFAULT_MAX_USD = 6.0
DEFAULT_ROUTE_COST = {"info_direct": 0.135, "llm": 0.385, "forced": 0.196, "other": 0.20}
ABSENCE_PHRASES = ("[NO ENCONTRADO", "no hay decisión registrada", "no existe", "no se encontr", "no encontr", "no hay ")
_NEGATION_RE = re.compile(r"\bno (existe|hay|se encontr|encontr|aparece|está|esta definid)", re.IGNORECASE)
_INT_RE = re.compile(r"(?<![\w:.])(\d{1,5})(?![\w.])")

VERDICT_OK, VERDICT_FAIL, VERDICT_NA = "acierto", "fallo", "no_evaluable"
STATE_OK, STATE_STALE, STATE_UNREVIEWED, STATE_SKIPPED = "ok", "caducado", "sin_revisar", "omitido_por_tope"


@dataclass
class RunResult:
    verdict: str
    cost_usd: float = 0.0
    router: str | None = None
    note: str = ""
    cited_files: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    id: str
    type: str
    state: str = STATE_OK
    verdict: str | None = None  # mediana sobre las corridas evaluables
    runs: list[RunResult] = field(default_factory=list)
    unstable: bool = False
    note: str = ""


# -- git grep ---------------------------------------------------------------------------------------


def _git_grep(root: Path, pattern: str, fixed: bool = False, count: bool = False) -> tuple[int, list[str]]:
    """(nº de líneas que coinciden, archivos). Fallback a grep si git falla."""
    flag = ["-F"] if fixed else ["-E"]
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "grep", "-I", "-c", *flag, "-e", pattern, "--", "."],
            capture_output=True, text=True, timeout=20,
        )
        if proc.returncode in (0, 1):
            files, total = [], 0
            for line in proc.stdout.splitlines():
                path, _, n = line.rpartition(":")
                if path:
                    files.append(path)
                    total += int(n or 0)
            return total, files
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    try:
        proc = subprocess.run(
            ["grep", "-rIc", *flag, "--exclude-dir=.git", "--exclude-dir=vendor", "-e", pattern, "--", str(root)],
            capture_output=True, text=True, timeout=20,
        )
        files, total = [], 0
        for line in proc.stdout.splitlines():
            path, _, n = line.rpartition(":")
            if n.isdigit() and int(n) > 0:
                files.append(os.path.relpath(path, root))
                total += int(n)
        return total, files
    except (OSError, subprocess.TimeoutExpired):
        return 0, []


def _roots(repos: dict) -> dict[str, Path]:
    return {alias: Path(str(p)).expanduser().resolve() for alias, p in (repos or {}).items() if p}


def _repo_relative(path: str, alias: str, root: Path) -> str:
    """`alias/x.go` o `<carpeta del repo>/x.go` → `x.go` (pathspec relativo al repo)."""
    rel = path.lstrip("./")
    for prefix in (f"{alias}/", f"{root.name}/"):
        if rel.startswith(prefix):
            return rel[len(prefix):]
    return rel


def _repo_for(case: EvalCase, roots: dict[str, Path]) -> list[tuple[str, Path]]:
    alias = (case.expect or {}).get("repo")
    if alias and alias in roots:
        return [(alias, roots[alias])]
    return list(roots.items())


# -- validación en frío ($0) --------------------------------------------------------------------------


def validate(case: EvalCase, repos: dict) -> tuple[str, str]:
    """(estado, nota) antes de gastar: sin_revisar, caducado u ok."""
    if case.review != "revisado":
        return STATE_UNREVIEWED, "borrador de --draft: revísalo y marca review=revisado"
    if case.type not in TYPES:
        return STATE_STALE, f"type desconocido: {case.type}"
    roots = _roots(repos)
    exp = case.expect or {}
    if case.type in COMMIT_TYPES and case.repo_commit:
        for alias, commit in case.repo_commit.items():
            root = roots.get(alias)
            if root is None:
                return STATE_STALE, f"repo {alias} no está en REPOS"
            head = git_head(root)
            if head and commit and not head.startswith(commit[: len(head)]) and not commit.startswith(head):
                paths = [_repo_relative(exp["file"], alias, root)] if exp.get("file") else []
                changed, why = changed_files(root, commit, paths or ["."])
                if changed is None:
                    return STATE_STALE, f"{alias}: {why}"
                if changed:
                    return STATE_STALE, f"{alias}: HEAD {head} ≠ {commit}; cambió {', '.join(changed[:3])}"
    if case.type == "location":
        if not exp.get("file") or not exp.get("symbol"):
            return STATE_STALE, "location exige expect.file y expect.symbol"
        index = cit.RepoIndex({a: str(r) for a, r in roots.items()})
        status, matches = index.resolve(exp["file"])
        if status != cit.STATUS_OK:
            return STATE_STALE, f"el archivo esperado no se resuelve ({status})"
        text = matches[0][1].read_text(encoding="utf-8", errors="ignore")
        if not re.search(rf"\b{re.escape(exp['symbol'])}\b", text):
            return STATE_STALE, "el símbolo esperado ya no está en el archivo"
        if not symbol_defined(text, exp["symbol"]):
            return STATE_STALE, "el símbolo se menciona pero no está DEFINIDO (func/type/const/var) en el archivo"
    if case.type in ("existence", "count"):
        if not exp.get("grep"):
            return STATE_STALE, f"{case.type} exige expect.grep"
        total = sum(_git_grep(root, exp["grep"], fixed=bool(exp.get("fixed")))[0] for _, root in _repo_for(case, roots))
        if case.type == "existence":
            want = exp.get("must_say", "existe") == "existe"
            if want != (total > 0):
                return STATE_STALE, f"el código vivo {'ya no' if want else 'ahora sí'} contiene el patrón"
        elif exp.get("n") is not None and abs(total - int(exp["n"])) > int(exp.get("tolerance", 0)):
            return STATE_STALE, f"el conteo real es {total}, el esperado {exp['n']}"
    return STATE_OK, ""


# -- puntuación determinista -----------------------------------------------------------------------


def symbol_defined(text: str, symbol: str) -> bool:
    """True si `symbol` está DEFINIDO en el archivo (no solo mencionado): Go func/método/type/
    const/var (sueltos o dentro de un bloque `const (`/`var (`), Python def/class, JS/TS
    function/class/const/let."""
    name = re.escape(symbol)
    patterns = (
        rf"^\s*func\s+(\([^)]*\)\s*)?{name}\s*[\[(]",      # func X( / func (r *T) X( / func X[T any](
        rf"^\s*type\s+{name}\b",
        rf"^\s*(const|var)\s+{name}\b",
        rf"^\s*{name}\s+[\w.\[\]*]+\s*=\s*",                 # dentro de const ( X Tipo = … )
        rf"^\s*{name}\s*=\s*",                                 # dentro de const ( X = … ) / var ( X = … )
        rf"^\s*(async\s+)?def\s+{name}\s*\(",
        rf"^\s*class\s+{name}\b",
        rf"^\s*(export\s+)?(async\s+)?function\s+{name}\s*\(",
        rf"^\s*(export\s+)?(const|let|var)\s+{name}\s*=",
    )
    return any(re.search(pat, text, re.MULTILINE) for pat in patterns)


def cited_files(output: str, roots: dict[str, Path]) -> dict[str, list[int]]:
    """Archivo real → líneas citadas, resolviendo alias y basename con RepoIndex."""
    index = cit.RepoIndex({a: str(r) for a, r in roots.items()})
    out: dict[str, list[int]] = {}
    for m in cit.CITATION_RE.finditer(output):
        path, line = m.group(1), m.group(2)
        status, matches = index.resolve(path)
        if status == cit.STATUS_OK and matches:
            real = str(matches[0][1])
            out.setdefault(real, [])
            if line:
                out[real].append(int(line))
    return out


def _symbol_near(path: str, lines: list[int], symbol: str) -> bool:
    try:
        source = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return False
    rx = re.compile(rf"\b{re.escape(symbol)}\b")
    for n in lines or []:
        start, stop = max(0, n - 1 - cit.SYMBOL_WINDOW), min(len(source), n + cit.SYMBOL_WINDOW)
        if rx.search("\n".join(source[start:stop])):
            return True
    return False


def score(case: EvalCase, output: str, repos: dict) -> RunResult:
    roots = _roots(repos)
    exp = case.expect or {}
    cited = cited_files(output, roots)
    files = sorted(os.path.relpath(f, r) for f in cited for r in roots.values() if f.startswith(str(r)))
    if case.type == "legacy":
        cov = keyword_coverage(output, case.must_mention)
        ok = cov >= float(exp.get("min_coverage", 1.0))
        return RunResult(VERDICT_OK if ok else VERDICT_FAIL, note=f"coverage {cov}", cited_files=files)
    if case.type == "existence":
        total = sum(_git_grep(root, exp["grep"], fixed=bool(exp.get("fixed")))[0] for _, root in _repo_for(case, roots))
        truth = total > 0
        negated = bool(_NEGATION_RE.search(output)) or "[NO ENCONTRADO" in output.upper()
        term = exp.get("term") or exp["grep"]
        affirmed = bool(cited) or re.search(re.escape(term), output, re.IGNORECASE) is not None
        if negated and not cited:
            claim = False
        elif affirmed and not negated:
            claim = True
        else:
            return RunResult(VERDICT_NA, note="la respuesta no afirma ni niega con claridad", cited_files=files)
        return RunResult(VERDICT_OK if claim == truth else VERDICT_FAIL, note=f"real={'existe' if truth else 'no existe'}", cited_files=files)
    if case.type == "location":
        if not cited:
            return RunResult(VERDICT_NA, note="sin citas archivo:línea", cited_files=files)
        want = exp["file"].lstrip("./")
        for real, lines in cited.items():
            if real.replace("\\", "/").endswith("/" + want) or real.endswith(want):
                if _symbol_near(real, lines, exp["symbol"]):
                    return RunResult(VERDICT_OK, cited_files=files)
                return RunResult(VERDICT_FAIL, note=f"cita el archivo pero el símbolo no está a ≤{cit.SYMBOL_WINDOW} líneas", cited_files=files)
        return RunResult(VERDICT_FAIL, note="ninguna cita apunta al archivo esperado", cited_files=files)
    if case.type == "count":
        total = sum(_git_grep(root, exp["grep"], fixed=bool(exp.get("fixed")))[0] for _, root in _repo_for(case, roots))
        clean = cit.CITATION_RE.sub(" ", output)
        nums = sorted({int(n) for n in _INT_RE.findall(clean)})
        if len(nums) != 1:
            return RunResult(VERDICT_NA, note=f"{len(nums)} números candidatos, se esperaba 1", cited_files=files)
        ok = abs(nums[0] - total) <= int(exp.get("tolerance", 0))
        return RunResult(VERDICT_OK if ok else VERDICT_FAIL, note=f"dijo {nums[0]}, real {total}", cited_files=files)
    if case.type == "absence":
        phrases = exp.get("must_say") or list(ABSENCE_PHRASES)
        if any(p.lower() in output.lower() for p in phrases):
            return RunResult(VERDICT_OK, cited_files=files)
        if cited:
            return RunResult(VERDICT_FAIL, note="afirma con citas algo que no debería existir", cited_files=files)
        return RunResult(VERDICT_NA, note="ni niega ni cita", cited_files=files)
    if case.type == "consistency":
        if not cited:
            return RunResult(VERDICT_NA, note="sin citas", cited_files=files)
        return RunResult(VERDICT_OK, note="se decide entre corridas", cited_files=files)
    return RunResult(VERDICT_NA, note=f"type desconocido {case.type}")


def consolidate(case: EvalCase, runs: list[RunResult]) -> tuple[str | None, bool]:
    """(veredicto mediana, inestable). consistency compara los archivos citados entre corridas."""
    if case.type == "consistency":
        sets = [frozenset(r.cited_files) for r in runs if r.verdict != VERDICT_NA]
        if len(sets) < 2:
            return (VERDICT_NA if runs else None), False
        min_j = float((case.expect or {}).get("min_jaccard", 1.0))
        base = sets[0]
        ok = all((len(base & s) / len(base | s) if base | s else 1.0) >= min_j for s in sets[1:])
        return (VERDICT_OK if ok else VERDICT_FAIL), not ok
    evaluable = [r.verdict for r in runs if r.verdict != VERDICT_NA]
    if not evaluable:
        return (VERDICT_NA if runs else None), False
    unstable = len(set(evaluable)) > 1
    hits = sum(1 for v in evaluable if v == VERDICT_OK)
    return (VERDICT_OK if hits * 2 >= len(evaluable) else VERDICT_FAIL), unstable


# -- costo proyectado --------------------------------------------------------------------------------


def route_medians(month: str | None = None, cfg=None) -> dict[str, float]:
    """Mediana real de costo por ruta en el mes (sin corridas de eval); defaults si no hay datos."""
    out = dict(DEFAULT_ROUTE_COST)
    try:
        records = [r for r in metrics.read_run_records(month=month or metrics.local_day("%Y-%m"), cfg=cfg)
                   if r.get("ok") and r.get("source") not in metrics.EXCLUDED_SOURCES]
    except Exception:  # noqa: BLE001
        return out
    by: dict[str, list[float]] = {}
    for r in records:
        by.setdefault(r.get("router") or "other", []).append(float(r.get("cost_usd") or 0))
    for route, costs in by.items():
        if costs:
            out[route] = round(statistics.median(costs), 4)
    return out


def expected_route(case: EvalCase) -> str:
    if case.forced_agent:
        return "forced"
    from minka.core.orchestrator import router

    return "info_direct" if router.is_simple_info_question(case.input) and not router.asks_report(case.input) else "llm"


def projected_cost(case: EvalCase, runs: int, medians: dict[str, float]) -> float:
    route = expected_route(case)
    return runs * float(medians.get(route, medians.get("other", DEFAULT_ROUTE_COST["other"])))


# -- runner ----------------------------------------------------------------------------------------


def default_runner(case: EvalCase) -> tuple[str, float, str | None]:
    """Sesión nueva por corrida: Orchestrator limpio, sin historial, source eval_quality."""
    from minka.core.orchestrator.orchestrator import Orchestrator

    orch = Orchestrator()
    orch.quiet = True
    orch.source = SOURCE
    previous = os.environ.get(SOURCE_ENV)
    os.environ[SOURCE_ENV] = SOURCE
    try:
        output = orch.run(case.input, forced_agent=case.forced_agent)
    finally:
        if previous is None:
            os.environ.pop(SOURCE_ENV, None)
        else:
            os.environ[SOURCE_ENV] = previous
    record = getattr(orch, "last_run_record", None) or {}
    return output or "", float(record.get("cost_usd") or 0.0), record.get("router")


def evaluate(
    cases: list[EvalCase],
    repos: dict,
    runs: int = 1,
    max_usd: float = DEFAULT_MAX_USD,
    runner=None,
    medians: dict[str, float] | None = None,
) -> dict:
    runner = runner or default_runner
    medians = medians or route_medians()
    spent = 0.0
    stopped = ""
    results: list[CaseResult] = []
    for case in cases:
        n = int(case.runs or runs)
        state, note = validate(case, repos)
        cr = CaseResult(case.id, case.type, state=state, note=note)
        if state != STATE_OK:
            results.append(cr)
            continue
        if stopped:
            cr.state = STATE_SKIPPED
            cr.note = stopped
            results.append(cr)
            continue
        projected = projected_cost(case, n, medians)
        if spent + projected > max_usd:
            stopped = f"tope ${max_usd:.2f}: gastado ${spent:.2f} + proyectado ${projected:.2f}"
            cr.state, cr.note = STATE_SKIPPED, stopped
            results.append(cr)
            continue
        for _ in range(n):
            try:
                output, cost, router_name = runner(case)
            except Exception as exc:  # noqa: BLE001 — una corrida caída no tumba el set
                cr.runs.append(RunResult(VERDICT_NA, note=f"error: {type(exc).__name__}"))
                continue
            rr = score(case, output, repos)
            rr.cost_usd, rr.router = cost, router_name
            cr.runs.append(rr)
            spent += cost
            if spent > max_usd:
                stopped = f"tope ${max_usd:.2f} superado: gastado ${spent:.2f}"
                break
        cr.verdict, cr.unstable = consolidate(case, cr.runs)
        results.append(cr)
        if stopped:
            continue
    evaluable = [r for r in results if r.state == STATE_OK and r.verdict in (VERDICT_OK, VERDICT_FAIL)]
    hits = sum(1 for r in evaluable if r.verdict == VERDICT_OK)
    return {
        "source": SOURCE,
        "runs": runs,
        "max_usd": max_usd,
        "spent_usd": round(spent, 4),
        "stopped": stopped or None,
        "cases": len(results),
        "evaluables": len(evaluable),
        "aciertos": hits,
        "tasa_acierto": round(hits / len(evaluable), 3) if evaluable else None,
        "caducados": sum(1 for r in results if r.state == STATE_STALE),
        "sin_revisar": sum(1 for r in results if r.state == STATE_UNREVIEWED),
        "omitidos_por_tope": sum(1 for r in results if r.state == STATE_SKIPPED),
        "no_evaluables": sum(1 for r in results if r.state == STATE_OK and r.verdict == VERDICT_NA),
        "inestables": sum(1 for r in results if r.unstable),
        "medianas_ruta": medians,
        "results": [asdict(r) for r in results],
    }


def format_report(report: dict) -> str:
    lines = [
        f"evals quality · {report['cases']} casos · evaluables {report['evaluables']} · aciertos {report['aciertos']}"
        f" ({report['tasa_acierto'] if report['tasa_acierto'] is not None else '—'}) · caducados {report['caducados']}"
        f" · sin revisar {report['sin_revisar']} · no evaluables {report['no_evaluables']} · inestables {report['inestables']}"
        f" · gasto ${report['spent_usd']:.2f} de ${report['max_usd']:.2f}"
    ]
    if report.get("stopped"):
        lines.append(f"  detenido: {report['stopped']}")
    for r in report["results"]:
        tag = r["verdict"] or r["state"]
        extra = f" · {r['note']}" if r.get("note") else ""
        lines.append(f"  {r['id']:<28} {r['type']:<12} {tag}{' (inestable)' if r['unstable'] else ''}{extra}")
    return "\n".join(lines)


# -- mantenimiento: --refresh y --draft --------------------------------------------------------------


def cases_path(config, project: str) -> Path:
    return Path(config.PROJECTS_DIR) / project / "evals" / "cases.json"


def refresh_commit(config, project: str, case_id: str) -> tuple[bool, str]:
    """Reescribe repo_commit del caso con el HEAD actual de sus repos (tras revisión humana)."""
    path = cases_path(config, project)
    if not path.is_file():
        return False, f"no existe {path}"
    data = json.loads(path.read_text(encoding="utf-8"))
    roots = _roots(getattr(config, "REPOS", {}))
    for raw in data.get("cases", []):
        if raw.get("id") != case_id:
            continue
        case = EvalCase.model_validate(raw)
        if case.type not in COMMIT_TYPES:
            return False, f"{case_id} es {case.type}: repo_commit solo aplica a {', '.join(COMMIT_TYPES)}"
        targets = _repo_for(case, roots)
        raw["repo_commit"] = {alias: git_head(root) for alias, root in targets if git_head(root)}
        raw["review"] = "revisado"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return True, f"{case_id}: repo_commit → {', '.join(f'{a}@{c}' for a, c in raw['repo_commit'].items())}"
    return False, f"no hay caso {case_id}"


def draft_cases(config, project: str, limit: int = 10) -> tuple[list[dict], str]:
    """Borradores desde knowledge/extracted (+ interfaces.md): location por cita con símbolo y
    existence por símbolo sin cita. Marcados review="sin revisar" y validados contra el código vivo."""
    kn = Path(config.PROJECTS_DIR) / project / "knowledge" / "extracted"
    roots = _roots(getattr(config, "REPOS", {}))
    index = cit.RepoIndex({a: str(r) for a, r in roots.items()})
    drafts: list[dict] = []
    seen: set[str] = set()
    if not kn.is_dir():
        return [], f"no existe {kn}"
    for md in sorted(kn.glob("*.md")):
        for line in md.read_text(encoding="utf-8", errors="ignore").splitlines():
            if len(drafts) >= limit:
                break
            symbols = [s.split(".")[-1] for s in cit.BACKTICK_SYMBOL_RE.findall(line)]
            cites = [m for m in cit.CITATION_RE.finditer(line) if m.group(2)]
            if not symbols:
                continue
            symbol = symbols[0]
            if symbol in seen or len(symbol) < 4:
                continue
            seen.add(symbol)
            if cites:
                status, matches = index.resolve(cites[0].group(1))
                if status != cit.STATUS_OK:
                    continue
                rel = index.relative(matches[0][0], matches[0][1])
                case = {"id": f"draft-location-{len(drafts) + 1}", "type": "location",
                        "input": f"¿Dónde se define `{symbol}`?", "expect": {"file": rel, "symbol": symbol},
                        "repo_commit": {matches[0][0]: git_head(roots[matches[0][0]])}}
            else:
                case = {"id": f"draft-existence-{len(drafts) + 1}", "type": "existence",
                        "input": f"¿Existe `{symbol}` en el código?", "expect": {"grep": symbol, "fixed": True, "must_say": "existe"}}
            case.update({"agent": "architecture_agent", "review": "sin revisar", "source": md.name})
            ec = EvalCase.model_validate({**case, "review": "revisado"})
            state, note = validate(ec, getattr(config, "REPOS", {}))
            case["valid_now"] = state == STATE_OK
            if note:
                case["validation_note"] = note
            drafts.append(case)
    path = cases_path(config, project)
    data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"cases": []}
    existing = {c.get("id") for c in data.get("cases", [])}
    new = [c for c in drafts if c["id"] not in existing]
    data.setdefault("cases", []).extend(new)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    valid = sum(1 for c in new if c["valid_now"])
    return new, f"{len(new)} borrador(es) escritos en {path} ({valid} se sostienen hoy, {len(new) - valid} no); todos 'sin revisar'"


def load_cases(project: str, config, template: bool = False) -> list[EvalCase]:
    if template:
        data = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
        return [EvalCase.model_validate(c) for c in data.get("cases", [])]
    return load_golden_set(project, Path(config.PROJECTS_DIR))
