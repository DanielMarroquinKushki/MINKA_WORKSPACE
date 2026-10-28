"""
orchestrator — Plan-and-Execute.

1. Recibe el pedido en lenguaje natural.
2. Planifica (planner): decide qué agente(s) y en qué orden.
3. Ejecuta los agentes en secuencia, pasando contexto entre ellos.
4. Consolida los resultados.

Las operaciones de escritura las maneja el propio development_agent vía su
ApprovalGate, así que el orquestador no necesita re-implementar el gate.
"""

import json

from rich.console import Console
from rich.panel import Panel

from config_loader import config
from core.orchestrator.planner import make_plan
from core.orchestrator.state_manager import StateManager
from core.schemas import ReviewDecision

console = Console()


class Orchestrator:
    def __init__(self, config=None):
        # config se acepta por compatibilidad con el prompt; la config real
        # viene de config_loader. Se importa perezosamente para no forzar la
        # creación de clientes Anthropic en --help.
        self.state = StateManager()
        self._agents: dict = {}
        # last_run_ok: False cuando el run no pudo ejecutarse (infra), no cuando
        # el resultado no gustó. Convención del proyecto: los fallos de
        # infraestructura se comunican por valor, no por excepción — así quien
        # llama (main.py, un script) decide el exit code sin atrapar boto3.
        self.last_run_ok: bool = True
        self.last_usage: dict = {"per_agent": [], "totals": {}}

    def _get_agent(self, name: str):
        """Lazy-init de agentes: solo se crean cuando se usan (evita crear el
        cliente Anthropic al importar / en --help)."""
        if name in self._agents:
            return self._agents[name]

        from core.agents.architecture_agent import ArchitectureAgent
        from core.agents.debug_agent import DebugAgent
        from core.agents.development_agent import DevelopmentAgent
        from core.agents.meta_agent import MetaAgent
        from core.agents.pr_review_agent import PRReviewAgent
        from core.agents.report_agent import ReportAgent

        registry = {
            "architecture_agent": ArchitectureAgent,
            "development_agent": DevelopmentAgent,
            "pr_review_agent": PRReviewAgent,
            "debug_agent": DebugAgent,
            "report_agent": ReportAgent,
            "meta_agent": MetaAgent,
        }
        cls = registry.get(name)
        if cls is None:
            raise KeyError(f"Agente desconocido: {name}")
        self._agents[name] = cls()
        return self._agents[name]

    # -- Persistencia del run (checkpoint/resume, best-effort) ----------------
    def _state_path(self):
        return config.OUTPUT_DIR / "state" / "current_run.json"

    def _save_state(self, task, plan, results, completed, fix_loop_ran) -> None:
        """Persiste el progreso del run. Best-effort: nunca rompe el flujo."""
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "task": task,
                        "plan": plan,
                        "results": results,
                        "state_history": self.state.steps,
                        "completed": completed,
                        "fix_loop_ran": fix_loop_ran,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception:  # noqa: BLE001 — persistencia best-effort
            pass

    def _load_state(self):
        try:
            path = self._state_path()
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
        return None

    def _clear_state(self) -> None:
        try:
            self._state_path().unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    def _record_usage(self, agent) -> None:
        """Acumula el usage real del agente en self.last_usage.

        Best-effort: la contabilidad de tokens nunca debe romper un run.
        """
        try:
            usage = getattr(agent, "last_usage", None)
            if not usage:
                return
            self.last_usage["per_agent"].append(usage)
            totals = self.last_usage["totals"]
            for key in (
                "requests",
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
                # Desglose del write por TTL: sin esto el costo del cache de 1h
                # se tarifa a 1.25x en bridge.py y queda subestimado.
                "cache_creation_5m",
                "cache_creation_1h",
            ):
                totals[key] = totals.get(key, 0) + usage.get(key, 0)
        except Exception:  # noqa: BLE001 — métricas best-effort
            pass

    def _run_forced(self, task: str, forced_agent: str) -> str:
        """Ejecuta un solo agente, saltando el planner.

        Existe para llamadas programáticas: sin planner no hay una llamada extra
        al LLM para decidir el routing, y el resultado es determinista.
        Devuelve un string de error (no lanza) si el agente no existe, para que
        quien llama nunca reciba un KeyError desde adentro del orquestador.
        """
        name = forced_agent if forced_agent.endswith("_agent") else f"{forced_agent}_agent"
        try:
            agent = self._get_agent(name)
        except KeyError:
            self.last_run_ok = False
            return f"Agente desconocido: {forced_agent}"

        self.state.reset()
        result = agent.run(task)
        self.state.record_step(name, task, result)
        self._record_usage(agent)
        return result

    def run(
        self,
        task: str,
        use_llm_planner: bool = True,
        resume: bool = True,
        forced_agent: str = None,
    ) -> str:
        self.last_run_ok = True
        self.last_usage = {"per_agent": [], "totals": {}}

        # Antes de una tarea larga: garantiza sesión AWS válida (solo Bedrock).
        # La Capa 3 (pausa elegante) vive dentro de ensure_valid_session().
        if getattr(config, "USE_BEDROCK", False):
            if not config.get_aws_session().ensure_valid_session():
                self.last_run_ok = False
                return (
                    "Sesión AWS expirada. Renueva con: "
                    f"aws sso login --profile {config.AWS_PROFILE}"
                )

        # Agente forzado: un solo paso, sin planner ni checkpoint/resume.
        if forced_agent:
            return self._run_forced(task, forced_agent)

        # Reanudar un run interrumpido de la MISMA tarea, o arrancar fresco.
        saved = self._load_state() if resume else None
        if saved and saved.get("task") == task:
            plan = saved["plan"]
            results: list[str] = list(saved.get("results", []))
            self.state.restore(saved.get("state_history", []))
            done = int(saved.get("completed", 0))
            fix_loop_ran = bool(saved.get("fix_loop_ran", False))
            console.print(f"[dim]Reanudando run: {done}/{len(plan)} pasos ya completados.[/dim]")
        else:
            self.state.reset()
            plan = make_plan(task, use_llm=use_llm_planner)
            results = []
            done = 0
            fix_loop_ran = False

        console.print(
            Panel(
                "\n".join(
                    f"{i}. [cyan]{s['agent']}[/cyan] — {s['task'][:80]}"
                    for i, s in enumerate(plan, 1)
                ),
                title="Plan de ejecución",
                border_style="green",
            )
        )

        for i in range(done, len(plan)):
            step = plan[i]
            # Si el fix-loop ya corrió, el pr_review interno ya revisó el
            # cambio — un step de pr_review inmediatamente después duplicaría
            # la revisión (y el costo).
            if fix_loop_ran and step["agent"] == "pr_review_agent":
                console.print(
                    "[dim]pr_review_agent del plan omitido: "
                    "el fix-loop ya incluyó la revisión.[/dim]"
                )
                fix_loop_ran = False
                self._save_state(task, plan, results, i + 1, fix_loop_ran)
                continue
            fix_loop_ran = False

            if step["agent"] == "development_agent" and getattr(config, "EVALUATOR_ENABLED", False):
                # Nivel 5+: development → pr_review → fix, max 3 iteraciones.
                # El registro de steps lo hace _fix_loop internamente.
                result = self._fix_loop(step["task"])
                fix_loop_ran = True
            else:
                agent = self._get_agent(step["agent"])
                # Inyecta contexto de pasos previos si hay varios steps.
                prev = self.state.context_for_next()
                task_with_ctx = f"{prev}\n\n---\n\nTarea: {step['task']}" if prev else step["task"]

                result = agent.run(task_with_ctx)
                self.state.record_step(step["agent"], step["task"], result)
                self._record_usage(agent)
            results.append(f"### [{step['agent']}]\n{result}")
            self._save_state(task, plan, results, i + 1, fix_loop_ran)

        self._clear_state()

        if getattr(config, "META_AGENT_ENABLED", False) and len(self.state.steps) >= 5:
            console.print(
                "[dim]Meta-agente disponible: escribe 'evalúa esta sesión' "
                "para obtener análisis de performance.[/dim]"
            )

        return "\n\n".join(results)

    def _fix_loop(self, original_task: str, max_iterations: int = 3) -> str:
        """
        Loop de corrección: development → pr_review → fix si hay bloqueantes.
        Máximo max_iterations intentos antes de reportar convergence_failure.
        """
        # Paridad con run(): el primer intento recibe el contexto de los
        # steps previos del plan (p.ej. análisis del architecture_agent).
        prev = self.state.context_for_next()
        if prev:
            original_task = f"{prev}\n\n---\n\nTarea: {original_task}"

        iteration = 0
        pending_findings = []

        while iteration < max_iterations:
            iteration += 1

            # 1. Development agent implementa
            dev_task = original_task
            if pending_findings:
                dev_task += "\n\nHallazgos a corregir de la iteración anterior:\n"
                dev_task += "\n".join(f"- {f}" for f in pending_findings)

            dev_result = self._get_agent("development_agent").run(dev_task)
            self.state.record_step("development_agent", dev_task, dev_result)

            # 2. Revisión del candidato: 4R structured (Nivel 5+) sobre el dev_result,
            #    con fallback a pr_review_agent + _review_decision.
            from core.agents.base_agent import build_llm_client
            from core.orchestrator.four_r_review import run_four_r_review

            four_r = run_four_r_review(dev_result, build_llm_client())
            if four_r is not None:
                console.print(
                    Panel(
                        four_r.summary,
                        title="[magenta]Revisión 4R[/magenta]",
                        border_style="magenta",
                    )
                )
                self.state.record_step("four_r_review", "Revisión 4R del candidato", four_r.summary)
                routing = four_r.routing_decision
                review_result = four_r.summary
                pending = four_r.blockers
            else:
                review_task = f"Revisa el último cambio aplicado. Contexto: {original_task}"
                review_result = self._get_agent("pr_review_agent").run(review_task)
                self.state.record_step("pr_review_agent", review_task, review_result)
                decision = self._review_decision(review_result)
                routing = decision.routing_decision
                pending = decision.blockers

            if routing == "aprobar":
                return f"✅ Completado en {iteration} iteración(es).\n\n{dev_result}"

            if routing == "escalar":
                return (
                    f"⚠️ Revisión escala este cambio — requiere revisión humana.\n\n"
                    f"Hallazgos:\n{review_result}"
                )

            # routing == "corregir" → usa los hallazgos y reintenta
            pending_findings = pending or ["Revisar hallazgos de la revisión"]

            if iteration < max_iterations:
                console.print(
                    f"[yellow]Iteración {iteration}/{max_iterations} — "
                    f"{len(pending_findings)} hallazgos a corregir[/yellow]"
                )

        # Convergence failure
        return (
            f"❌ No convergió en {max_iterations} iteraciones.\n"
            f"Últimos hallazgos pendientes:\n"
            + "\n".join(f"- {f}" for f in pending_findings)
            + "\n\nRevisa manualmente y aplica los cambios necesarios."
        )

    def _review_decision(self, review_result: str) -> ReviewDecision:
        """Decisión de routing del pr_review vía salida estructurada; si falla,
        cae al heurístico por strings (comportamiento previo)."""
        try:
            from core.agents.base_agent import build_llm_client, resolve_model_id
            from core.structured import call_structured

            client = build_llm_client()
            model = resolve_model_id(
                config.MODELS.get(
                    "pr_review_agent", getattr(config, "DEFAULT_MODEL", config.ANTHROPIC_MODEL)
                )
            )
            decision = call_structured(
                client,
                model,
                system=(
                    "Extrae la decisión de routing de una revisión de PR. "
                    "aprobar = sin bloqueantes; corregir = hay hallazgos bloqueantes "
                    "a arreglar; escalar = requiere revisión humana."
                ),
                messages=[
                    {"role": "user", "content": f"Revisión del pr_review_agent:\n\n{review_result}"}
                ],
                schema_model=ReviewDecision,
                tool_name="submit_review_decision",
                tool_description="Decisión de routing y hallazgos bloqueantes de la revisión.",
            )
            if decision is not None:
                return decision
        except Exception:  # noqa: BLE001 — cae al heurístico
            pass
        # Fallback: heurístico por strings (comportamiento previo).
        return ReviewDecision(
            routing_decision=self._extract_routing_decision(review_result),
            blockers=self._extract_blockers(review_result),
        )

    def _extract_routing_decision(self, review_result: str) -> str:
        """Fallback heurístico: extrae routing_decision del texto del pr_review
        cuando la extracción estructurada (_review_decision) no está disponible."""
        result_lower = review_result.lower()
        if "routing_decision: aprobar" in result_lower:
            return "aprobar"
        if "routing_decision: escalar" in result_lower:
            return "escalar"
        if "routing_decision: corregir" in result_lower:
            return "corregir"
        # Default: si hay BLOQUEANTE → corregir, si no → aprobar
        if "bloqueante" in result_lower:
            return "corregir"
        return "aprobar"

    def _extract_blockers(self, review_result: str) -> list[str]:
        """Fallback heurístico: extrae hallazgos BLOQUEANTES del texto del
        pr_review cuando la extracción estructurada no está disponible."""
        blockers = []
        lines = review_result.split("\n")
        for i, line in enumerate(lines):
            if "BLOQUEANTE" in line.upper():
                # Incluye la línea del bloqueante y la siguiente si existe
                blockers.append(line.strip())
                if i + 1 < len(lines) and lines[i + 1].strip():
                    blockers.append(lines[i + 1].strip())
        return blockers or ["Revisar hallazgos del pr_review_agent"]
