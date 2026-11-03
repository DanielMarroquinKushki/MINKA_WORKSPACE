"""routing_evals — mide el planner LLM con los casos de información del golden set.

Solo llama al planner (`planner._llm_plan`), nunca crea agentes. Por caso y corrida
registra: resultado (ok | empty_plan | validation | api | transient | no_tool_block),
primer agente, acuerdo con el esperado, tokens y costo (core/pricing.py).
"""

import json
from collections import Counter
from pathlib import Path

from minka.config_loader import config
from minka.core.orchestrator import planner
from minka.core.orchestrator.router import (
    CURRENT_REQUEST_MARKER,
    is_simple_info_question,
    split_request,
)
from minka.core.pricing import PRICING_VERSION, cost_usd

MAX_CASES = 12
CASES_PATH = Path(__file__).resolve().parent.parent / "evals" / "routing_cases.json"


def info_cases(path: Path = CASES_PATH, limit: int = MAX_CASES) -> list[dict]:
    cases = json.loads(path.read_text("utf-8"))["cases"]
    return [c for c in cases if c.get("eval") == "info"][:limit]


def _context(case: dict) -> str:
    turns = case.get("turns") or []
    if not turns:
        return ""
    history = "\n".join(f"[Turno {i}] Dev: {t['request']}" for i, t in enumerate(turns, 1))
    return split_request(f"{history}\n---\n\n{CURRENT_REQUEST_MARKER}\n{case['request']}")[0]


def run_case(case: dict) -> dict:
    """Una llamada al planner; números y nombres, nunca el texto del pedido."""
    res = planner._llm_plan(case["request"], _context(case))
    expected = (case.get("expect") or {}).get("agents") or []
    first = res.steps[0]["agent"] if res.steps else None
    usage = res.usage or {}
    return {
        "outcome": "ok" if res.steps else (res.error.kind if res.error else "empty_plan"),
        "first_agent": first,
        "agrees": bool(first) and bool(expected) and first == expected[0],
        "retries": res.retries,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": cost_usd(usage) or 0.0,
    }


def evaluate(cases: list[dict], runs: int, runner=run_case) -> dict:
    per_case = []
    outcomes: Counter = Counter()
    agree = stable = calls = 0
    tokens_in = tokens_out = 0
    cost = 0.0
    for case in cases:
        results = [runner(case) for _ in range(runs)]
        agents = [r["first_agent"] for r in results]
        case_outcomes = Counter(r["outcome"] for r in results)
        outcomes.update(case_outcomes)
        case_agree = sum(r["agrees"] for r in results)
        is_stable = len(set(agents)) == 1
        agree += case_agree
        stable += is_stable
        calls += len(results)
        tokens_in += sum(r["input_tokens"] for r in results)
        tokens_out += sum(r["output_tokens"] for r in results)
        cost += sum(r["cost_usd"] for r in results)
        per_case.append(
            {
                "id": case["id"],
                "expected": ((case.get("expect") or {}).get("agents") or [None])[0],
                "outcomes": dict(case_outcomes),
                "agents": agents,
                "agree": f"{case_agree}/{runs}",
                "stable": is_stable,
                "retries": sum(r["retries"] for r in results),
            }
        )
    direct = [c["id"] for c in cases if getattr(config, "PLANNER_SKIP_INFO", True) and is_simple_info_question(c["request"])]
    return {
        "cases": len(cases),
        "runs": runs,
        "calls": calls,
        "direct_without_planner": len(direct),
        "direct_ids": direct,
        "empty_plan_rate": outcomes["empty_plan"] / calls if calls else 0.0,
        "planner_errors": {k: v for k, v in outcomes.items() if k not in ("ok", "empty_plan")},
        "agreement_rate": agree / calls if calls else 0.0,
        "stability_rate": stable / len(cases) if cases else 0.0,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "cost_usd": round(cost, 4),
        "pricing_version": PRICING_VERSION,
        "per_case": per_case,
    }


def format_report(report: dict) -> str:
    lines = [
        f"casos {report['cases']} × {report['runs']} corridas = {report['calls']} llamadas · "
        f"empty_plan {report['empty_plan_rate']:.0%} · acuerdo {report['agreement_rate']:.0%} · "
        f"estables {report['stability_rate']:.0%} · errores {report['planner_errors'] or 'ninguno'} · "
        f"tokens {report['input_tokens']}/{report['output_tokens']} · ${report['cost_usd']:.4f} · "
        f"sin planner en producción: {report['direct_without_planner']}/{report['cases']}",
    ]
    for c in report["per_case"]:
        agents = ", ".join((a or "—").removesuffix("_agent") for a in c["agents"])
        lines.append(
            f"  {c['id']:<32} esperado {str(c['expected']).removesuffix('_agent'):<13} "
            f"acuerdo {c['agree']}  {'estable' if c['stable'] else 'inestable'}  [{agents}]"
        )
    return "\n".join(lines)
