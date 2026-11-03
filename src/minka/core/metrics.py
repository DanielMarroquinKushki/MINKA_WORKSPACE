"""metrics — registro por corrida (solo números y nombres) y resumen para `stats`.

Un JSONL por día local en OUTPUT_DIR/metrics/runs/. No guarda la tarea ni el
resultado: esa traza de depuración sigue en output/logs/agent_runs.jsonl.
"""

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from minka.config_loader import config
from minka.core.pricing import PRICING_VERSION, baseline_usd, cost_usd

# Las llamadas del planner, 4R y _review_decision aún no se contabilizan.
SCOPE_NOTE = "(sin planner/4R)"

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
) -> dict:
    per_agent = []
    cost = base = 0.0
    priced = True
    for u in usage.get("per_agent", []):
        entry = {"agent": u.get("agent"), "model": u.get("model")}
        entry.update({k: u.get(k) or 0 for k in TOKEN_KEYS})
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
        "plan": list(plan),
        "fix_loop_iters": fix_loop_iters,
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


def aggregate(records: list[dict], project: str | None = None, agent: str | None = None) -> dict:
    """Totales y desglose por agente. Con `agent`, los totales son solo de ese agente."""
    if project:
        records = [r for r in records if r.get("project") == project]
    if agent:
        agent = _short(agent)
        records = [
            r for r in records if any(_short(u.get("agent")) == agent for u in r.get("per_agent", []))
        ]
    agents: dict[str, dict] = {}
    costs: dict[str, list[float]] = {}
    totals = dict.fromkeys(TOKEN_KEYS, 0)
    cost = base = duration = 0.0
    ok = fix_runs = fix_iters = 0
    routers: Counter = Counter()
    for r in records:
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
