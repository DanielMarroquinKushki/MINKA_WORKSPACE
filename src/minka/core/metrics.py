"""metrics — registro por corrida (solo números y nombres) y resumen para `stats`.

Un JSONL por día local en OUTPUT_DIR/metrics/runs/. No guarda la tarea ni el
resultado: esa traza de depuración sigue en output/logs/agent_runs.jsonl.
"""

import json
import math
import os
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from minka.config_loader import config
from minka.core.pricing import PRICING_VERSION, baseline_usd, cost_usd

# Las llamadas del planner, 4R y _review_decision aún no se contabilizan.
SCOPE_NOTE = "(sin planner/4R)"

# Fuentes que solo se ven pidiéndolas con --source (evaluaciones, no uso real).
EXCLUDED_SOURCES = frozenset({"eval_quality"})

TOKEN_KEYS = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "cache_creation_5m",
    "cache_creation_1h",
)


def local_day(fmt: str = "%Y-%m-%d") -> str:
    """Fecha del reloj local: el archivo del día coincide con el día del dev, no con UTC."""
    return datetime.now(timezone.utc).astimezone().strftime(fmt)


def runs_dir(cfg=None) -> Path:
    return Path((cfg or config).OUTPUT_DIR) / "metrics" / "runs"


def baseline_path(cfg=None) -> Path:
    return Path((cfg or config).OUTPUT_DIR) / "metrics" / "baseline_v3.json"


def mkdir_private(path: Path) -> None:
    """Directorio 0700 (se crea y se ajusta aunque ya existiera con permisos más abiertos)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def append_private(path: Path, line: str) -> None:
    """Añade una línea a un archivo 0600 dentro de un directorio 0700 (patrón de trace_log)."""
    mkdir_private(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def write_private(path: Path, text: str) -> None:
    mkdir_private(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def cache_ratio(tokens: dict) -> float | None:
    """cache_read / (input + cache_read + cache_creation); None sin tokens de entrada."""
    read = tokens.get("cache_read_input_tokens") or 0
    total = (tokens.get("input_tokens") or 0) + read + (tokens.get("cache_creation_input_tokens") or 0)
    return read / total if total else None


def safe_cwd(path: str) -> str:
    """Ruta del cwd sin el home del usuario: `~/...` en vez de `/Users/<nombre>/...`."""
    try:
        home = str(Path.home())
        if path == home or path.startswith(home + os.sep):
            return "~" + path[len(home):]
    except (OSError, RuntimeError):
        pass
    return path


def build_run_record(
    *,
    run_id: str,
    source: str,
    project: str | None,
    cwd: str,
    router: str | None,
    plan: list[str],
    fix_loop_iters: int,
    ok: bool,
    duration_s: float,
    usage: dict,
    reason: str | None = None,
    confidence: str | None = None,
    planner_error: dict | None = None,
    reasoning: str | None = None,
    planner_retries: int = 0,
    ends_with_offer: bool = False,
    closings_removed: int = 0,
    citations_total: int = 0,
    citations_missing: int = 0,
    citations_ok: int = 0,
    citations_symbol_far: int = 0,
    citations_symbol_absent: int = 0,
    prompt_hash: str | None = None,
    response_lines: int = 0,
    claims: dict | None = None,
    detalle_views: int = 0,
    branches_off_base: int = 0,
    budget_gate: str | None = None,
    estimate_shown: bool = False,
    profile: str | None = None,
    absences: dict | None = None,
) -> dict:
    per_agent = []
    cost = base = 0.0
    priced = True
    for u in usage.get("per_agent", []):
        entry = {"agent": u.get("agent"), "model": u.get("model")}
        entry.update({k: u.get(k) or 0 for k in TOKEN_KEYS})
        entry["repeated_tool_calls"] = u.get("repeated_tool_calls") or 0
        entry["iterations_capped"] = u.get("iterations_capped") or 0
        per_agent.append(entry)
        c = cost_usd(u)
        if c is None:
            priced = False
        else:
            cost += c
            base += baseline_usd(u) or 0.0
    totals = usage.get("totals") or {}
    return {
        "run_id": run_id,
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "project": project,
        "cwd": cwd,
        "router": router,
        "reason": reason,
        "confidence": confidence,
        "planner_error": planner_error,
        "reasoning": reasoning,
        "planner_retries": planner_retries,
        "repeated_tool_calls": sum(e["repeated_tool_calls"] for e in per_agent),
        # Agentes que llegaron al tope de vueltas y respondieron con lo recogido.
        "iterations_capped": sum(e["iterations_capped"] for e in per_agent),
        "plan": list(plan),
        "fix_loop_iters": fix_loop_iters,
        # Cierre "¿Quieres que…?": si la respuesta lo traía y cuántas líneas se recortaron.
        "ends_with_offer": bool(ends_with_offer),
        "closings_removed": int(closings_removed),
        # Citas archivo:línea de la respuesta verificadas contra REPOS (citation_check.py).
        "citations_total": int(citations_total),
        "citations_missing": int(citations_missing),
        # Paso 12: cita resuelta con el símbolo cerca (ok), en otra parte del archivo o ausente.
        "citations_ok": int(citations_ok),
        "citations_symbol_far": int(citations_symbol_far),
        "citations_symbol_absent": int(citations_symbol_absent),
        # Líneas de la respuesta mostrada (número, nunca el texto).
        "response_lines": int(response_lines),
        # Verificador de afirmaciones (claim_check.py): comprobadas, sin respaldo, bloques
        # marcados como propuesta y respuestas cortadas por MAX_TOKENS.
        "claims_checked": int((claims or {}).get("checked") or 0),
        "claims_unsupported": int((claims or {}).get("unsupported") or 0),
        "blocks_proposed": int((claims or {}).get("blocks_proposed") or 0),
        "claims_doc": int((claims or {}).get("doc") or 0),
        # Historial de decisiones (core/memory/decisions.py): entradas usadas en el bloque user y
        # referencias ADR de la respuesta sin respaldo en el decisions_log.
        "decisions_used": int((claims or {}).get("decisions_used") or 0),
        "decisions_unsupported": int((claims or {}).get("decisions_unsupported") or 0),
        # Verificador: herramientas nombradas sin ejecutar e inconsistencias con corridas previas.
        "claims_inferred": int((claims or {}).get("inferred") or 0),
        "claims_inconsistent": int((claims or {}).get("inconsistent") or 0),
        # Ids R-nn de la respuesta que no existen en el risk_catalog.md del proyecto.
        "risks_unsupported": int((claims or {}).get("risks_unsupported") or 0),
        # Corridas interrumpidas por red/timeout/throttling/credenciales (parcial) y respuestas vacías.
        "interrupted": int(totals.get("interrupted") or 0),
        "empty_responses": int(totals.get("empty_response") or 0),
        # Seguridad en tools (core/security.py): archivos excluidos y redacciones por tipo, sin valores.
        "security_excluded": int(totals.get("security_excluded") or 0),
        "redactions": {k: int(v) for k, v in (totals.get("redactions") or {}).items()},
        "truncated_outputs": int((claims or {}).get("truncated") or 0),
        # Paso 12b: uso de /detalle desde la corrida anterior, repos fuera de su rama base, freno de
        # presupuesto (kind) que cortó la corrida, si se mostró estimado previo y el perfil de respuesta.
        "detalle_views": int(detalle_views),
        "branches_off_base": int(branches_off_base),
        "budget_gate": budget_gate,
        "estimate_shown": bool(estimate_shown),
        "profile": profile,
        # Paso 12b parte 2 (absence_check.py): ausencias dichas ("no existe", "no hay", "[NO ENCONTRADO"),
        # refutadas (el nombre existe en el repo) y no evaluables. Se completan tras mostrar la respuesta
        # (amend_run_record); solo conteo, nunca se marca la respuesta.
        "absences_said": int((absences or {}).get("absences_said") or 0),
        "absences_refuted": int((absences or {}).get("absences_refuted") or 0),
        "absences_unclear": int((absences or {}).get("absences_unclear") or 0),
        # Huella de prompts + tools + modelos (core/baseline.py): misma línea base ⇔ mismo hash.
        "prompt_hash": prompt_hash,
        "ok": ok,
        "duration_s": round(duration_s, 3),
        "tokens": {k: totals.get(k) or 0 for k in TOKEN_KEYS},
        "per_agent": per_agent,
        "cost_usd": round(cost, 6) if priced else None,
        "baseline_usd": round(base, 6) if priced else None,
        "pricing_version": PRICING_VERSION,
    }


def run_cost_usd(usage: dict) -> float:
    """Costo acumulado de los agentes de la corrida (0 para modelos sin tarifa)."""
    return sum(cost_usd(u) or 0.0 for u in usage.get("per_agent", []))


def route_key(router: str | None, steps: int) -> str:
    """Clave del estimado previo: ruta y nº de pasos del plan ("llm/2")."""
    return f"{router or 'other'}/{int(steps)}"


def route_median_counts(month: str | None = None, cfg=None, by_steps: bool = False) -> dict[str, tuple[float, int]]:
    """{ruta: (mediana de cost_usd, corridas)} del mes, solo corridas ok y fuera de EXCLUDED_SOURCES.
    Con `by_steps`, la clave es `route_key(ruta, len(plan))`: un plan de 2 pasos no se estima con la
    mediana de los de 1. Datos reales únicamente: sin defaults, sin factores. {} si no hay registros."""
    try:
        records = [
            r for r in read_run_records(month=month or local_day("%Y-%m"), cfg=cfg)
            if r.get("ok") and r.get("source") not in EXCLUDED_SOURCES and r.get("cost_usd") is not None
        ]
    except Exception:  # noqa: BLE001
        return {}
    by: dict[str, list[float]] = {}
    for r in records:
        key = route_key(r.get("router"), len(r.get("plan") or [])) if by_steps else (r.get("router") or "other")
        by.setdefault(key, []).append(float(r.get("cost_usd") or 0))
    return {route: (round(statistics.median(costs), 4), len(costs)) for route, costs in by.items() if costs}


def write_run_record(record: dict, cfg=None) -> bool:
    """Best-effort: la contabilidad nunca rompe un run."""
    try:
        target = runs_dir(cfg)
        mkdir_private(target.parent)
        append_private(target / f"{local_day()}.jsonl", json.dumps(record, ensure_ascii=False))
        return True
    except Exception:  # noqa: BLE001
        return False


def amend_run_record(run_id: str, fields: dict, cfg=None) -> bool:
    """Completa campos de un registro ya escrito (hoy o ayer, por si la corrida cruzó medianoche):
    reescribe esa línea del JSONL (0600). Best-effort: False si no lo encontró o no pudo escribir."""
    if not run_id or not fields:
        return False
    today = datetime.now(timezone.utc).astimezone().date()
    for day in (today, today - timedelta(days=1)):
        path = runs_dir(cfg) / f"{day.isoformat()}.jsonl"
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for i in range(len(lines) - 1, -1, -1):
            try:
                rec = json.loads(lines[i])
            except json.JSONDecodeError:
                continue
            if rec.get("run_id") != run_id:
                continue
            rec.update(fields)
            lines[i] = json.dumps(rec, ensure_ascii=False)
            try:
                write_private(path, "\n".join(lines) + "\n")
            except OSError:
                return False
            return True
    return False


_DAY_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")


def _day_files(cfg=None) -> list[tuple[date, Path]]:
    out = []
    for p in sorted(runs_dir(cfg).glob("*.jsonl")):
        m = _DAY_FILE_RE.match(p.name)
        if not m:
            continue
        try:
            out.append((date.fromisoformat(m.group(1)), p))
        except ValueError:
            continue
    return out


def _count_lines(path: Path) -> int:
    try:
        return sum(1 for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip())
    except OSError:
        return 0


def rotate_runs(cfg=None, today: date | None = None) -> tuple[int, int]:
    """Retira los archivos de output/metrics/runs con más de METRICS_KEEP_DAYS días (el archivo
    completo, por día). Devuelve (archivos, líneas) retirados. Nunca toca baseline_v3.json ni nada
    fuera de runs/."""
    cfg = cfg or config
    keep = int(getattr(cfg, "METRICS_KEEP_DAYS", 90) or 0)
    if keep <= 0:
        return 0, 0
    cutoff = (today or date.today()) - timedelta(days=keep)
    files = lines = 0
    for day, path in _day_files(cfg):
        if day < cutoff:
            n = _count_lines(path)
            try:
                path.unlink()
            except OSError:
                continue
            files += 1
            lines += n
    return files, lines


def rotation_notice(files: int, lines: int, cfg=None) -> str:
    keep = int(getattr(cfg or config, "METRICS_KEEP_DAYS", 90) or 0)
    return f"metrics/runs: {lines} línea(s) en {files} archivo(s) con más de {keep} días retiradas"


PURGE_KINDS = ("metricas", "logs", "feedback", "todo")


@dataclass
class PurgeItem:
    kind: str  # metricas | logs | feedback
    path: Path
    lines: int  # líneas que se retirarían
    whole: bool  # True: se borra el archivo; False: solo las líneas anteriores a --before


def _line_day(line: str) -> date | None:
    """Fecha del campo "ts" de una línea JSONL (ISO), sin parsear todo el JSON."""
    i = line.find('"ts": "')
    if i < 0:
        return None
    raw = line[i + 7:].split('"', 1)[0]
    try:
        return datetime.fromisoformat(raw).date()
    except ValueError:
        return None


def _old_lines(path: Path, before: date) -> int:
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return 0
    return sum(1 for ln in lines if ln.strip() and (d := _line_day(ln)) is not None and d < before)


def purge_plan(cfg=None, before: date | None = None, kinds: str = "todo") -> list[PurgeItem]:
    """Qué borraría `stats --purge --que <kinds>`: metricas = runs/<día>.jsonl (archivos completos de
    días anteriores a `before`), logs = logs/agent_runs.jsonl(.1), feedback = evals/*_feedback.jsonl de
    cada proyecto. Con `before`, en logs y feedback solo las líneas con `ts` anterior (el archivo queda).
    Sin fecha: todo. Nunca el baseline."""
    cfg = cfg or config
    if kinds not in PURGE_KINDS:
        raise ValueError(f"--que debe ser uno de {', '.join(PURGE_KINDS)}")
    want = set(PURGE_KINDS[:-1]) if kinds == "todo" else {kinds}
    out: list[PurgeItem] = []
    if "metricas" in want:
        for day, path in _day_files(cfg):
            if before is None or day < before:
                out.append(PurgeItem("metricas", path, _count_lines(path), True))
    files: list[tuple[str, Path]] = []
    if "logs" in want:
        logs = Path(cfg.OUTPUT_DIR) / "logs"
        files += [("logs", logs / n) for n in ("agent_runs.jsonl", "agent_runs.jsonl.1")]
    if "feedback" in want:
        projects = Path(getattr(cfg, "PROJECTS_DIR", Path(cfg.OUTPUT_DIR) / "no"))
        if projects.is_dir():
            files += [("feedback", p) for p in sorted(projects.glob("*/evals/*_feedback.jsonl"))]
    for kind, p in files:
        if not p.is_file():
            continue
        if before is None:
            out.append(PurgeItem(kind, p, _count_lines(p), True))
        else:
            n = _old_lines(p, before)
            if n:
                out.append(PurgeItem(kind, p, n, False))
    return out


def purge(plan: list[PurgeItem], before: date | None = None) -> int:
    """Ejecuta el plan. Devuelve archivos tocados (borrados o reescritos sin las líneas viejas)."""
    touched = 0
    for item in plan:
        try:
            if item.whole or before is None:
                item.path.unlink()
            else:
                lines = item.path.read_text(encoding="utf-8", errors="ignore").splitlines()
                kept = [ln for ln in lines if not (ln.strip() and (d := _line_day(ln)) is not None and d < before)]
                write_private(item.path, ("\n".join(kept) + "\n") if kept else "")
            touched += 1
        except OSError:
            continue
    return touched


# -- línea base v3 y categorías de salud -----------------------------------------------------------

BASELINE_V3_RANGE = ("2026-11-05", "2026-11-11")
BASELINE_V3_NOTE = (
    "Referencia previa al paso 12, no una meta: corridas ok del REPL/CLI del 2026-11-05 al 2026-11-11, "
    "sin eval_quality. 'Citas verificadas' cambió de significado en 0.2.4 (símbolo cerca de la cita): "
    "citations_total/missing son los comparables; citations_ok/symbol_* no existían en este rango."
)


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 4) if values else None


def health_categories(records: list[dict], feedback: list[dict] | None = None) -> dict[str, dict]:
    """{categoría: {value, n}} sobre corridas ok sin eval_quality. Solo números. `feedback`: entradas de
    response_feedback.jsonl (verdict bien|mal) del mismo rango, para "/bien sobre /bien+/mal"."""
    recs = [r for r in records if r.get("ok") and r.get("source") not in EXCLUDED_SOURCES]
    out: dict[str, dict] = {}
    by_route: dict[str, list[float]] = {}
    for r in recs:
        if r.get("cost_usd") is None:
            continue
        key = route_key(r.get("router"), len(r.get("plan") or []))
        if key.startswith("llm/") and int(key.split("/")[1]) >= 2:
            key = "llm/2+"
        by_route.setdefault(key, []).append(float(r["cost_usd"]))
    for key in ("info_direct/1", "llm/1", "llm/2+", "forced/1"):
        vals = by_route.get(key, [])
        out[f"costo_mediano_{key}"] = {"value": _median(vals), "n": len(vals)}
    caches = [c for c in (cache_ratio(r.get("tokens") or {}) for r in recs) if c is not None]
    out["cache_mediana"] = {"value": _median(caches), "n": len(caches)}
    with_cit = [r for r in recs if r.get("citations_total")]
    total = sum(r["citations_total"] for r in with_cit)
    out["citas_no_encontradas_pct"] = {
        "value": round(sum(r.get("citations_missing") or 0 for r in with_cit) / total, 4) if total else None,
        "n": total,
    }
    sym = sum((r.get("citations_symbol_far") or 0) + (r.get("citations_symbol_absent") or 0) for r in with_cit)
    out["citas_simbolo_lejano_o_ausente_pct"] = {"value": round(sym / total, 4) if total else None, "n": total}
    n = len(recs)
    out["interrumpidas_pct"] = {"value": round(sum(1 for r in recs if r.get("interrupted")) / n, 4) if n else None, "n": n}
    out["vueltas_con_tope_pct"] = {"value": round(sum(1 for r in recs if r.get("iterations_capped")) / n, 4) if n else None, "n": n}
    out["cierres_con_oferta_pct"] = {"value": round(sum(1 for r in recs if r.get("ends_with_offer")) / n, 4) if n else None, "n": n}
    # Paso 12b parte 2: ausencias refutadas sobre las evaluables (dichas − no evaluables) y /bien sobre /bien+/mal.
    evaluable = sum((r.get("absences_said") or 0) - (r.get("absences_unclear") or 0) for r in recs)
    refuted = sum(r.get("absences_refuted") or 0 for r in recs)
    out["ausencias_refutadas_pct"] = {"value": round(refuted / evaluable, 4) if evaluable > 0 else None, "n": max(evaluable, 0)}
    verdicts = [e.get("verdict") for e in (feedback or []) if e.get("verdict") in ("bien", "mal")]
    out["bien_sobre_total"] = {"value": round(verdicts.count("bien") / len(verdicts), 4) if verdicts else None, "n": len(verdicts)}
    out["corridas"] = {"value": n, "n": n}
    return out


def records_between(start: str, end: str, cfg=None) -> list[dict]:
    """Registros de los días [start, end] (ISO), inclusive."""
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out: list[dict] = []
    day = d0
    while day <= d1:
        out.extend(read_run_records(day=day.isoformat(), cfg=cfg))
        day += timedelta(days=1)
    return out


def freeze_baseline(cfg=None, force: bool = False, start: str = BASELINE_V3_RANGE[0], end: str = BASELINE_V3_RANGE[1]) -> dict:
    """Escribe output/metrics/baseline_v3.json (0600) con las categorías de salud del rango.
    No sobrescribe uno existente sin `force` (FileExistsError)."""
    cfg = cfg or config
    path = baseline_path(cfg)
    if path.exists() and not force:
        raise FileExistsError(str(path))
    payload = {
        "name": "v3",
        "range": [start, end],
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "note": BASELINE_V3_NOTE,
        "categories": health_categories(records_between(start, end, cfg)),
    }
    write_private(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return payload


def read_run_records(day: str | None = None, month: str | None = None, cfg=None) -> list[dict]:
    """Registros del día (default: hoy, fecha local) o del mes (YYYY-MM)."""
    pattern = f"{month}-*.jsonl" if month else f"{day or local_day()}.jsonl"
    records = []
    for path in sorted(runs_dir(cfg).glob(pattern)):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def _p90(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(math.ceil(0.9 * len(ordered)) - 1, 0)]


def _short(agent: str | None) -> str:
    return (agent or "?").removesuffix("_agent")


def aggregate(
    records: list[dict],
    project: str | None = None,
    agent: str | None = None,
    source: str | None = None,
) -> dict:
    """Totales y desglose por agente. Con `agent`, los totales son solo de ese agente;
    `source` filtra por quién lanzó la corrida (repl|cli|bridge|<etiqueta MINKA_SOURCE>)."""
    if project:
        records = [r for r in records if r.get("project") == project]
    if source:
        records = [r for r in records if r.get("source") == source]
    else:
        # Las corridas de `minka evals quality` no entran en los agregados del día/mes.
        records = [r for r in records if r.get("source") not in EXCLUDED_SOURCES]
    if agent:
        agent = _short(agent)
        records = [
            r for r in records if any(_short(u.get("agent")) == agent for u in r.get("per_agent", []))
        ]
    agents: dict[str, dict] = {}
    costs: dict[str, list[float]] = {}
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    cost = base = duration = 0.0
    ok = fix_runs = fix_iters = repeated = capped = 0
    claims_checked = claims_unsupported = blocks_proposed = truncated_outputs = claims_doc = 0
    security_excluded = decisions_used = decisions_unsupported = claims_inferred = claims_inconsistent = 0
    interrupted = empty_responses = risks_unsupported = 0
    absences_said = absences_refuted = absences_unclear = 0
    redactions: Counter = Counter()
    offers = closings = cit_total = cit_missing = cit_ok = cit_far = cit_absent = 0
    routers: Counter = Counter()
    prompt_hashes: Counter = Counter()
    for r in records:
        repeated += r.get("repeated_tool_calls") or 0
        capped += r.get("iterations_capped") or 0
        claims_checked += r.get("claims_checked") or 0
        claims_unsupported += r.get("claims_unsupported") or 0
        blocks_proposed += r.get("blocks_proposed") or 0
        claims_doc += r.get("claims_doc") or 0
        security_excluded += r.get("security_excluded") or 0
        decisions_used += r.get("decisions_used") or 0
        decisions_unsupported += r.get("decisions_unsupported") or 0
        claims_inferred += r.get("claims_inferred") or 0
        claims_inconsistent += r.get("claims_inconsistent") or 0
        risks_unsupported += r.get("risks_unsupported") or 0
        interrupted += r.get("interrupted") or 0
        empty_responses += r.get("empty_responses") or 0
        absences_said += r.get("absences_said") or 0
        absences_refuted += r.get("absences_refuted") or 0
        absences_unclear += r.get("absences_unclear") or 0
        redactions.update({k: int(v) for k, v in (r.get("redactions") or {}).items()})
        truncated_outputs += r.get("truncated_outputs") or 0
        if r.get("prompt_hash"):
            prompt_hashes[r["prompt_hash"]] += 1
        offers += 1 if r.get("ends_with_offer") else 0
        closings += r.get("closings_removed") or 0
        cit_total += r.get("citations_total") or 0
        cit_missing += r.get("citations_missing") or 0
        cit_ok += r.get("citations_ok") or 0
        cit_far += r.get("citations_symbol_far") or 0
        cit_absent += r.get("citations_symbol_absent") or 0
        for u in r.get("per_agent", []):
            name = _short(u.get("agent"))
            if agent and name != agent:
                continue
            row = agents.setdefault(name, {"runs": 0, "cost_usd": 0.0, **dict.fromkeys(TOKEN_KEYS, 0)})
            row["runs"] += 1
            for k in TOKEN_KEYS:
                row[k] += u.get(k) or 0
            c = cost_usd(u) or 0.0
            row["cost_usd"] += c
            costs.setdefault(name, []).append(c)
            if agent:
                for k in TOKEN_KEYS:
                    totals[k] += u.get(k) or 0
                cost += c
                base += baseline_usd(u) or 0.0
        if not agent:
            for k in TOKEN_KEYS:
                totals[k] += (r.get("tokens") or {}).get(k) or 0
            cost += r.get("cost_usd") or 0.0
            base += r.get("baseline_usd") or 0.0
        duration += r.get("duration_s") or 0.0
        ok += 1 if r.get("ok") else 0
        routers[r.get("router") or "?"] += 1
        if r.get("fix_loop_iters"):
            fix_runs += 1
            fix_iters += r["fix_loop_iters"]
    for name, row in agents.items():
        row["p90_usd"] = _p90(costs[name])
    return {
        "runs": len(records),
        "ok": ok,
        "duration_s": duration,
        "tokens": totals,
        "cost_usd": cost,
        "baseline_usd": base,
        "agents": agents,
        "routers": dict(routers),
        "fix_loop_runs": fix_runs,
        "fix_loop_iters": fix_iters,
        "repeated_tool_calls": repeated,
        "iterations_capped": capped,
        "claims_checked": claims_checked,
        "claims_unsupported": claims_unsupported,
        "blocks_proposed": blocks_proposed,
        "claims_doc": claims_doc,
        "security_excluded": security_excluded,
        "redactions": dict(redactions),
        "decisions_used": decisions_used,
        "decisions_unsupported": decisions_unsupported,
        "claims_inferred": claims_inferred,
        "claims_inconsistent": claims_inconsistent,
        "risks_unsupported": risks_unsupported,
        "interrupted": interrupted,
        "empty_responses": empty_responses,
        "absences_said": absences_said,
        "absences_refuted": absences_refuted,
        "absences_unclear": absences_unclear,
        "truncated_outputs": truncated_outputs,
        "ends_with_offer": offers,
        "closings_removed": closings,
        "citations_total": cit_total,
        "citations_missing": cit_missing,
        "citations_ok": cit_ok,
        "citations_symbol_far": cit_far,
        "citations_symbol_absent": cit_absent,
        "prompt_hashes": dict(prompt_hashes),
    }


def fmt_tokens(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def fmt_usd(value: float | None) -> str:
    return "$–" if value is None else f"${value:.2f}"


def fmt_cache(tokens: dict) -> str:
    ratio = cache_ratio(tokens)
    return "cache –" if ratio is None else f"cache {ratio:.0%}"


def format_run_summary(record: dict) -> str:
    """Línea final de una corrida, sin markup: apta para stderr."""
    tokens = record.get("tokens") or {}
    total = sum(
        tokens.get(k) or 0
        for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    )
    agents = "→".join(_short(a) for a in record.get("plan") or []) or (record.get("router") or "?")
    if (record.get("fix_loop_iters") or 0) > 1:
        agents += f" (fix-loop ×{record['fix_loop_iters']})"
    return (
        f"{fmt_tokens(total)} tokens · {fmt_cache(tokens)} · {fmt_usd(record.get('cost_usd'))} · "
        f"{record.get('duration_s') or 0:.0f} s · agentes: {agents} {SCOPE_NOTE}"
    )
