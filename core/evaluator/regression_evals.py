"""
regression_evals — evaluación de regresión de calidad de los agentes (Nivel 5).

Corre cada agente sobre un "golden set" por proyecto
(projects/<x>/evals/cases.json) y puntúa su salida con dos métricas:
- keyword_coverage: determinista (¿menciona lo que debe?) — pura, offline.
- G-Eval (DeepEval, LLM-judge): puntúa contra una rúbrica — requiere LLM (SSO).

Gate por EVALUATOR_ENABLED. El loader y la métrica determinista son puros y
testeables sin red; los runs (agente + LLM-judge) necesitan sesión LLM.

Este es un módulo NUEVO y propio: no reemplaza a agent_evaluator (Nivel 6,
basado en logs) ni a code_evaluator (Nivel 5, calidad de código) — son features
distintas.
"""

import json
from pathlib import Path

from pydantic import BaseModel, Field


class EvalCase(BaseModel):
    """Un caso del golden set."""

    id: str
    agent: str
    input: str
    must_mention: list[str] = Field(default_factory=list)
    rubric: str = ""


def load_golden_set(project: str, base_dir: Path | None = None) -> list[EvalCase]:
    """Carga projects/<project>/evals/cases.json. Devuelve [] si no existe."""
    from config_loader import config

    root = base_dir or Path(config.BASE_DIR)
    path = root / "projects" / project / "evals" / "cases.json"
    if not path.is_file():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [EvalCase.model_validate(c) for c in data.get("cases", [])]


def keyword_coverage(output: str, must_mention: list[str]) -> float:
    """Fracción [0..1] de los términos must_mention presentes en `output`
    (case-insensitive). 1.0 si must_mention está vacío."""
    if not must_mention:
        return 1.0
    low = output.lower()
    hits = sum(1 for term in must_mention if term.lower() in low)
    return round(hits / len(must_mention), 3)


def geval_detail(question: str, output: str, rubric: str) -> dict | None:
    """Puntúa con DeepEval G-Eval (juez Bedrock) y devuelve {'score', 'reason'}.

    La `reason` explica QUÉ le faltó a la salida — insumo del prompt_optimizer.
    Devuelve None si deepeval/juez/red no están disponibles.
    """
    try:
        from deepeval.metrics import GEval
        from deepeval.models import AmazonBedrockModel
        from deepeval.test_case import LLMTestCase, LLMTestCaseParams

        from config_loader import config

        judge = AmazonBedrockModel(
            model_id=getattr(
                config, "EVAL_JUDGE_MODEL", "us.anthropic.claude-3-haiku-20240307-v1:0"
            ),
            region=getattr(config, "AWS_REGION", "us-east-1"),
        )
        metric = GEval(
            name="Rubrica",
            criteria=rubric,
            model=judge,
            evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        )
        metric.measure(LLMTestCase(input=question, actual_output=output))
        return {"score": round(float(metric.score), 3), "reason": str(metric.reason or "")}
    except Exception:  # noqa: BLE001 — sin deepeval/juez/red → None
        return None


def _geval_score(question: str, output: str, rubric: str) -> float | None:
    """Solo el score de G-Eval (reusa geval_detail). None si no disponible."""
    detail = geval_detail(question, output, rubric)
    return detail["score"] if detail else None


def run_evaluation(project: str, use_geval: bool = True) -> dict:
    """
    Corre el golden set del proyecto contra los agentes y puntúa cada caso.

    Requiere sesión LLM (los agentes llaman al LLM) y, para G-Eval, un modelo
    juez configurado. Gate: EVALUATOR_ENABLED. Devuelve un reporte serializable.
    """
    from config_loader import config

    if not getattr(config, "EVALUATOR_ENABLED", False):
        return {"error": "EVALUATOR_ENABLED=False — activa el evaluador (Nivel 5)."}
    cases = load_golden_set(project)
    if not cases:
        return {"error": f"Sin golden set para '{project}' (projects/{project}/evals/cases.json)."}

    # Gate de sesión AWS NO bloqueante (solo Bedrock): si no hay sesión válida,
    # falla rápido con instrucciones — no pausa esperando (a diferencia del
    # orchestrator, que sí espera para tareas largas).
    if getattr(config, "USE_BEDROCK", False):
        if not config.get_aws_session().sso_token_status().get("valid"):
            return {
                "error": "Sesión AWS expirada. Renueva con: "
                f"aws sso login --profile {config.AWS_PROFILE} y reintenta."
            }

    from core.orchestrator.orchestrator import Orchestrator

    orch = Orchestrator(config=config)
    results = []
    for case in cases:
        try:
            output = orch._get_agent(case.agent).run(case.input)
        except Exception as exc:  # noqa: BLE001 — un caso que falla no tumba el reporte
            results.append({"id": case.id, "agent": case.agent, "error": str(exc)[:200]})
            continue
        entry = {
            "id": case.id,
            "agent": case.agent,
            "keyword_coverage": keyword_coverage(output, case.must_mention),
        }
        if use_geval and case.rubric:
            entry["geval"] = _geval_score(case.input, output, case.rubric)
        results.append(entry)
    return {"project": project, "cases": len(results), "results": results}
