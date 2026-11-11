"""metrics — registro por corrida (solo números y nombres) y resumen para `stats`.

Un JSONL por día local en OUTPUT_DIR/metrics/runs/. No guarda la tarea ni el
resultado: esa traza de depuración sigue en output/logs/agent_runs.jsonl.
"""

import json
import math
import os
from collections import Counter
from datetime import datetime, timezone
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
    prompt_hash: str | None = None,
    response_lines: int = 0,
    claims: dict | None = None,
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
        # Seguridad en tools (core/security.py): archivos excluidos y redacciones por tipo, sin valores.
        "security_excluded": int(totals.get("security_excluded") or 0),
        "redactions": {k: int(v) for k, v in (totals.get("redactions") or {}).items()},
        "truncated_outputs": int((claims or {}).get("truncated") or 0),
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


def write_run_record(record: dict, cfg=None) -> bool:
    """Best-effort: la contabilidad nunca rompe un run."""
    try:
        target = runs_dir(cfg)
        target.mkdir(parents=True, exist_ok=True)
        with (target / f"{local_day()}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:  # noqa: BLE001
        return False


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
    redactions: Counter = Counter()
    offers = closings = cit_total = cit_missing = 0
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
        redactions.update({k: int(v) for k, v in (r.get("redactions") or {}).items()})
        truncated_outputs += r.get("truncated_outputs") or 0
        if r.get("prompt_hash"):
            prompt_hashes[r["prompt_hash"]] += 1
        offers += 1 if r.get("ends_with_offer") else 0
        closings += r.get("closings_removed") or 0
        cit_total += r.get("citations_total") or 0
        cit_missing += r.get("citations_missing") or 0
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
        "truncated_outputs": truncated_outputs,
        "ends_with_offer": offers,
        "closings_removed": closings,
        "citations_total": cit_total,
        "citations_missing": cit_missing,
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
