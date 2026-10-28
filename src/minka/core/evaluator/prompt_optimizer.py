"""
prompt_optimizer — optimización reflexiva de system prompts (Nivel 6).

Usa el feedback de los evals (keyword_coverage + términos faltantes + score y
RAZÓN del juez G-Eval) para que un meta-LLM proponga un prompt mejorado. NUNCA
auto-aplica: devuelve una propuesta tipo ADR para aprobación humana (el dev la
aplica al *_prompt.py y la registra en decisions_log.md).

Respeta la arquitectura: no reescribe agentes, solo propone texto de prompt.
Gate: META_AGENT_ENABLED + PROMPT_OPTIMIZATION_ENABLED. Los runs (agentes +
juez + meta-LLM) requieren sesión LLM.
"""

from minka.core.evaluator.regression_evals import geval_detail, keyword_coverage, load_golden_set
from minka.core.prompt_loader import load_system_prompt


def _meta_system(heading: str) -> str:
    return (
        "Eres un optimizador de prompts. Recibes el system prompt ACTUAL de un agente "
        "y el feedback de una evaluación de calidad (qué le faltó a sus respuestas). "
        "Propón una versión MEJORADA del prompt que corrija esas debilidades, "
        "CONSERVANDO toda su estructura, secciones y reglas existentes (no elimines "
        f"'Lo que NUNCA hago', 'Manejo de errores', '{heading}', el routing, etc.). "
        "Devuelve SOLO el texto del prompt mejorado, sin comentarios ni explicaciones."
    )


def _missed_keywords(output: str, must_mention: list[str]) -> list[str]:
    """Términos de must_mention ausentes en output (case-insensitive). Puro."""
    low = output.lower()
    return [t for t in must_mention if t.lower() not in low]


class PromptOptimizer:
    def __init__(self, config):
        if not (config.META_AGENT_ENABLED and config.PROMPT_OPTIMIZATION_ENABLED):
            raise NotImplementedError(
                "Optimización de prompts desactivada. Requiere META_AGENT_ENABLED y "
                "PROMPT_OPTIMIZATION_ENABLED = True (Nivel 6)."
            )
        self.config = config

    def _collect_feedback(self, agent_name: str, project: str) -> tuple[list, list[dict]]:
        """Corre los golden cases del agente en vivo y recolecta feedback por caso."""
        cases = [c for c in load_golden_set(project) if c.agent == agent_name]
        if not cases:
            return cases, []

        from minka.core.orchestrator.orchestrator import Orchestrator

        orch = Orchestrator(config=self.config)
        feedback = []
        for case in cases:
            try:
                output = orch._get_agent(case.agent).run(case.input)
            except Exception as exc:  # noqa: BLE001 — un caso que falla no tumba el resto
                feedback.append({"id": case.id, "error": str(exc)[:200]})
                continue
            detail = geval_detail(case.input, output, case.rubric) if case.rubric else None
            feedback.append(
                {
                    "id": case.id,
                    "keyword_coverage": keyword_coverage(output, case.must_mention),
                    "missed_keywords": _missed_keywords(output, case.must_mention),
                    "geval_score": (detail or {}).get("score"),
                    "geval_reason": (detail or {}).get("reason", ""),
                }
            )
        return cases, feedback

    def propose(self, agent_name: str, project: str) -> dict:
        """
        Corre los evals del agente y propone un prompt mejorado (ADR) vía meta-LLM.
        Requiere sesión LLM. NO escribe nada — la decisión final es del dev.
        """
        # Gate de sesión AWS (solo Bedrock): login solo si el token venció y hay
        # TTY; si no, aborta con el mensaje (sin navegador ni esperas).
        if getattr(self.config, "USE_BEDROCK", False):
            ok, msg = self.config.get_aws_session().ensure_sso_login()
            if not ok:
                return {"error": msg}

        cases, feedback = self._collect_feedback(agent_name, project)
        if not cases:
            return {"error": f"Sin golden cases para '{agent_name}' en '{project}'."}

        current_prompt = load_system_prompt(agent_name)
        weak = [f for f in feedback if not f.get("error")]
        fb_text = "\n\n".join(
            f"[Caso {f['id']}] cobertura={f['keyword_coverage']} "
            f"faltó={f['missed_keywords']} geval={f['geval_score']}\n"
            f"Razón del juez: {f['geval_reason'][:400]}"
            for f in weak
        )

        from minka.core.agents.base_agent import build_llm_client, resolve_model_id

        client = build_llm_client()
        model = resolve_model_id(
            self.config.MODELS.get(
                "meta_agent", getattr(self.config, "DEFAULT_MODEL", self.config.ANTHROPIC_MODEL)
            )
        )
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=self.config.MAX_TOKENS,
                system=_meta_system(
                    getattr(self.config, "TEAM_PILLARS_HEADING", "Pilares del equipo")
                ),
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"PROMPT ACTUAL de {agent_name}:\n\n{current_prompt}\n\n"
                            f"FEEDBACK DE EVALS (debilidades a corregir):\n{fb_text}\n\n"
                            "Devuelve el prompt mejorado."
                        ),
                    }
                ],
            )
            proposed = " ".join(b.text for b in resp.content if hasattr(b, "text")).strip()
        except Exception as exc:  # noqa: BLE001 — no propagar fallos de infra al dev
            return {"error": f"Meta-LLM falló: {exc}"}

        return {
            "agent": agent_name,
            "project": project,
            "baseline": [
                {
                    "id": f["id"],
                    "keyword_coverage": f.get("keyword_coverage"),
                    "geval_score": f.get("geval_score"),
                }
                for f in feedback
            ],
            "current_prompt": current_prompt,
            "proposed_prompt": proposed,
            "note": (
                "Propuesta ADR — revisa y, si te convence, aplícala al *_prompt.py del "
                "proyecto y regístrala en decisions_log.md. Luego re-corre --evaluate "
                "para confirmar que el score sube."
            ),
        }
