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
import os
import uuid
from time import perf_counter

from rich.console import Console
from rich.panel import Panel

from minka._app import QUIET_ENV, SOURCE_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.memory.conversation_memory import ConversationMemory
from minka.core.orchestrator import approval_gate, response_profile, router
from minka.core.orchestrator.state_manager import StateManager
from minka.core.schemas import ReviewDecision

console = Console()


# Agentes disponibles (orden del saludo y claves del registro de _get_agent).
AGENT_NAMES = (
    "architecture_agent",
    "development_agent",
    "pr_review_agent",
    "debug_agent",
    "report_agent",
    "meta_agent",
)


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
        # Métricas de la última corrida (ver core/metrics.py). `source` lo fija
        # quien crea el orquestador (main.py: repl|cli); SOURCE_ENV lo pisa.
        self.source: str = "cli"
        self.last_run_id: str | None = None
        self.last_router: str | None = None
        self.last_plan: list[str] = []
        self.fix_loop_iters: int = 0
        self.last_run_record: dict = {}
        # Router (core/orchestrator/router.py): decisión del último run y turnos de
        # la sesión (pedido recortado + agentes) para estado y seguimientos.
        self.last_reason: str | None = None
        self.last_confidence: str | None = None
        self.last_decision: router.RouteDecision | None = None
        self.last_exit_code: int | None = None
        # Historial ÚNICO de la sesión, compartido por todos los agentes (solo en memoria).
        self.memory = ConversationMemory(
            max_turns=getattr(config, "HISTORY_MAX_TURNS", 5),
            result_chars=getattr(config, "HISTORY_RESULT_CHARS", 1500),
        )
        # Los fija main.py: --quiet apaga la línea de decisión (stderr);
        # --read-only (o MINKA_SOURCE=bridge) rechaza agentes que escriben.
        self.quiet: bool = False
        self.read_only: bool = False
        self._stderr = Console(stderr=True)
        # Concisión por tipo de pedido (response_profile.py). force_full lo fijan
        # --completo y --debug; los contadores del cierre van al registro de la corrida.
        self.force_full: bool = False
        self.last_profile: str = response_profile.PROFILE_FULL
        self.last_ends_with_offer: bool = False
        self.last_closings_removed: int = 0

    def clear_session(self) -> None:
        """Olvida el historial de la sesión (REPL /clear)."""
        self.memory.clear()

    @property
    def session_turns(self) -> list[dict]:
        """Vista para el router: pedido recortado, agentes, si fue útil y sus citas."""
        return [
            {
                "request": t.request[:120],
                "agents": list(t.agents),
                "useful": t.useful,
                "citations": list(t.citations),
            }
            for t in self.memory.turns
        ]

    @session_turns.setter
    def session_turns(self, turns: list[dict]) -> None:
        self.memory.clear()
        for t in turns:
            self.memory.add(t.get("request", ""), t.get("agents") or [], t.get("result", ""), t.get("useful", True))

    def _record_turn(self, request: str, agents: list[str], useful: bool, result: str = "") -> str:
        """Gancho post-respuesta. Todo turno cuenta (también los de $0): la herencia mira
        el inmediatamente anterior. Aquí se recorta el cierre "¿Quieres que…?" (una línea,
        patrón cerrado, nunca con cita) y se guarda el resultado recortado y sin cabeceras
        `### [agente]`. Devuelve el texto que debe mostrarse."""
        result, offered, removed = response_profile.strip_closing_offer(result)
        self.last_ends_with_offer = self.last_ends_with_offer or offered
        self.last_closings_removed += removed
        self.memory.add(request, agents, result, useful)
        return result

    def _get_agent(self, name: str):
        """Lazy-init de agentes: solo se crean cuando se usan (evita crear el
        cliente Anthropic al importar / en --help)."""
        if name in self._agents:
            return self._agents[name]

        from minka.core.agents.architecture_agent import ArchitectureAgent
        from minka.core.agents.debug_agent import DebugAgent
        from minka.core.agents.development_agent import DevelopmentAgent
        from minka.core.agents.meta_agent import MetaAgent
        from minka.core.agents.pr_review_agent import PRReviewAgent
        from minka.core.agents.report_agent import ReportAgent

        classes = (ArchitectureAgent, DevelopmentAgent, PRReviewAgent, DebugAgent, ReportAgent, MetaAgent)
        registry = dict(zip(AGENT_NAMES, classes))
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
                "repeated_tool_calls",
            ):
                totals[key] = totals.get(key, 0) + usage.get(key, 0)
        except Exception:  # noqa: BLE001 — métricas best-effort
            pass

    def _run_forced(self, task: str, request: str, forced_agent: str) -> str:
        """Ejecuta un solo agente, saltando el router (override --agent).

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

        self._apply_decision(
            router.RouteDecision("forced", "override --agent", [name], confidence="alta")
        )
        self.last_plan = [name]
        self.state.reset()
        agent.conversation = self.memory.as_messages()
        # Forzado (--agent, bridge): siempre completo, sin perfil breve.
        self.last_profile = response_profile.profile_for("forced", name, force_full=True)
        agent.response_profile = self.last_profile
        result = agent.run(task)
        self.state.record_step(name, task, result)
        self._record_usage(agent)
        return self._record_turn(request, [name], router.is_useful_result(result), result)

    def run(
        self,
        task: str,
        use_llm_planner: bool = True,
        resume: bool = True,
        forced_agent: str = None,
    ) -> str:
        self.last_run_ok = True
        self.last_usage = {"per_agent": [], "totals": {}}
        self.last_run_id = uuid.uuid4().hex
        self.last_router = None
        self.last_reason = None
        self.last_confidence = None
        self.last_decision = None
        self.last_exit_code = None
        self.last_plan = []
        self.fix_loop_iters = 0
        self.last_profile = response_profile.PROFILE_FULL
        self.last_ends_with_offer = False
        self.last_closings_removed = 0
        started = perf_counter()
        try:
            return self._run(task, use_llm_planner, resume, forced_agent)
        except BaseException:
            self.last_run_ok = False
            raise
        finally:
            self.last_run_record = self._close_run_record(started)

    def _close_run_record(self, started: float) -> dict:
        """Registro de la corrida: números y nombres, nunca la tarea ni el resultado.

        Best-effort: si falla devuelve {} y el run ya terminó igual.
        """
        try:
            record = metrics.build_run_record(
                run_id=self.last_run_id,
                source=os.getenv(SOURCE_ENV) or self.source,
                project=getattr(config, "ACTIVE_PROJECT", None),
                cwd=os.getcwd(),
                router=self.last_router,
                plan=self.last_plan,
                fix_loop_iters=self.fix_loop_iters,
                ok=self.last_run_ok,
                duration_s=perf_counter() - started,
                usage=self.last_usage,
                reason=self.last_reason,
                confidence=self.last_confidence,
                planner_error=getattr(self.last_decision, "planner_error", None),
                reasoning=getattr(self.last_decision, "reasoning", None),
                planner_retries=getattr(self.last_decision, "retries", 0),
                ends_with_offer=self.last_ends_with_offer,
                closings_removed=self.last_closings_removed,
            )
        except Exception:  # noqa: BLE001
            return {}
        metrics.write_run_record(record)
        return record

    def _apply_decision(self, decision: router.RouteDecision) -> None:
        """Guarda la decisión del router y la anuncia en una línea (stderr)."""
        self.last_decision = decision
        self.last_router = decision.router
        self.last_reason = decision.reason
        self.last_confidence = decision.confidence
        if self.quiet or os.getenv(QUIET_ENV):
            return
        self._stderr.print(
            router.describe(decision), markup=False, highlight=False, soft_wrap=True, style="dim"
        )

    def _run(
        self,
        task: str,
        use_llm_planner: bool,
        resume: bool,
        forced_agent: str | None,
    ) -> str:
        # El historial que antepone main.py no se enruta: solo el pedido actual.
        context, request = router.split_request(task)
        repos = getattr(config, "REPOS", {})

        # Un run interrumpido de la MISMA tarea se reanuda tal cual, sin re-enrutar.
        saved = self._load_state() if (resume and not forced_agent) else None
        resuming = bool(saved and saved.get("task") == task)

        # Capas 1 y 4 (costo 0, sin sesión AWS): saludo, estado, meta, ambigüedad.
        decision = None
        if not forced_agent and not resuming:
            decision = router.local_decision(request, self.session_turns, AGENT_NAMES, repos)
            if decision is not None and decision.reply is not None:
                self._apply_decision(decision)
                self._record_turn(request, [], useful=False)
                return decision.reply

        # Antes de una tarea: sesión AWS utilizable (solo Bedrock). Login solo si
        # el token venció; sin TTY o sin red devuelve el mensaje, sin esperar.
        if getattr(config, "USE_BEDROCK", False):
            ok, msg = config.get_aws_session().ensure_sso_login()
            if not ok:
                self.last_run_ok = False
                return msg

        # Agente forzado: un solo paso, sin planner ni checkpoint/resume.
        if forced_agent:
            return self._run_forced(task, request, forced_agent)

        # Reanudar un run interrumpido de la MISMA tarea, o arrancar fresco.
        if resuming:
            plan = saved["plan"]
            results: list[str] = list(saved.get("results", []))
            self.state.restore(saved.get("state_history", []))
            done = int(saved.get("completed", 0))
            fix_loop_ran = bool(saved.get("fix_loop_ran", False))
            self._apply_decision(
                router.RouteDecision("resume", "run reanudado", [s["agent"] for s in plan])
            )
            console.print(f"[dim]Reanudando run: {done}/{len(plan)} pasos ya completados.[/dim]")
        else:
            self.state.reset()
            if decision is None:  # capas 2 y 3: planner + reglas duras
                decision = router.plan_decision(
                    request,
                    context or self.memory.planner_context(),
                    use_llm_planner,
                    self.session_turns,
                    repos,
                )
            self._apply_decision(decision)
            if decision.reply is not None:  # planner sin plan: pide precisar
                self._record_turn(request, [], useful=False)
                return decision.reply
            if self.read_only and any(a not in router.READONLY_AGENTS for a in decision.agents):
                self.last_run_ok = False
                self.last_exit_code = 2
                self._record_turn(request, [], useful=False)
                return router.read_only_message(decision.agents)
            # Solo el plan del LLM trae tareas propias; keyword/herencia/directo reciben la
            # tarea completa (con historial) más el sufijo del router, si lo hay.
            if decision.router == "llm":
                plan = decision.steps
            else:
                suffix = f"\n\n{decision.task_suffix}" if decision.task_suffix else ""
                plan = [{"agent": a, "task": f"{task}{suffix}"} for a in decision.agents]
            results = []
            done = 0
            fix_loop_ran = False
        self.last_plan = [step["agent"] for step in plan]

        console.print(
            Panel(
                "\n".join(
                    # Solo el pedido, sin el prefijo de historial que antepone main.py.
                    f"{i}. [cyan]{s['agent']}[/cyan] — {router.split_request(s['task'])[1][:80]}"
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

            # Nivel 5+: fix-loop solo con decisión de confianza alta y verbo de
            # escritura explícito; si no, development corre una vez, sin revisión.
            fix_loop_ok = (
                step["agent"] == "development_agent"
                and getattr(config, "EVALUATOR_ENABLED", False)
                and self.last_confidence == "alta"
                and router.has_write_verb(request)
            )
            if fix_loop_ok:
                # development → revisión → fix, max 3 iteraciones.
                # El registro de steps lo hace _fix_loop internamente.
                result = self._fix_loop(step["task"])
                fix_loop_ran = True
            else:
                agent = self._get_agent(step["agent"])
                # Inyecta contexto de pasos previos si hay varios steps.
                prev = self.state.context_for_next()
                task_with_ctx = f"{prev}\n\n---\n\nTarea: {step['task']}" if prev else step["task"]

                agent.conversation = self.memory.as_messages()
                # Perfil por tipo de pedido: info_direct → breve; lo demás completo.
                # "en detalle"/"completo" en el pedido, --completo y --debug lo fuerzan.
                self.last_profile = response_profile.profile_for(
                    self.last_router,
                    step["agent"],
                    force_full=self.force_full or response_profile.wants_full(request),
                )
                agent.response_profile = self.last_profile
                result = agent.run(task_with_ctx)
                self.state.record_step(step["agent"], step["task"], result)
                self._record_usage(agent)
            results.append(f"### [{step['agent']}]\n{result}")
            self._save_state(task, plan, results, i + 1, fix_loop_ran)

        self._clear_state()
        final = "\n\n".join(results)
        # Se guarda la salida consolidada SIN cabeceras (las quita la memoria); se muestra con ellas.
        final = self._record_turn(request, list(self.last_plan), router.is_useful_result(result), final)
        # La nota "respuesta acotada" es solo para mostrar: no va a la memoria ni al modelo.
        final = response_profile.with_bounded_note(final, self.last_profile)

        if getattr(config, "META_AGENT_ENABLED", False) and len(self.state.steps) >= 5:
            console.print(
                "[dim]Meta-agente disponible: escribe 'evalúa esta sesión' "
                "para obtener análisis de performance.[/dim]"
            )

        return final

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
        consecutive_fixes = 0
        max_usd = float(getattr(config, "FIX_LOOP_MAX_USD", 0) or 0)
        # Los intentos previos de este mismo fix-loop viajan como historial intra-corrida.
        base_history = self.memory.as_messages()
        intra_history: list[dict] = []

        while iteration < max_iterations:
            iteration += 1
            self.fix_loop_iters = iteration

            # 1. Development agent implementa
            dev_task = original_task
            if pending_findings:
                dev_task += "\n\nHallazgos a corregir de la iteración anterior:\n"
                dev_task += "\n".join(f"- {f}" for f in pending_findings)

            approval_gate.reset_request_count()
            dev_agent = self._get_agent("development_agent")
            dev_agent.conversation = base_history + intra_history
            dev_result = dev_agent.run(dev_task)
            intra_history += [
                {"role": "user", "content": dev_task},
                {"role": "assistant", "content": dev_result},
            ]
            self.state.record_step("development_agent", dev_task, dev_result)
            self._record_usage(dev_agent)

            # Sin diff propuesto no hay cambio que revisar: la revisión costaría
            # cuatro llamadas sobre texto.
            if approval_gate.request_count() == 0:
                return dev_result
            if max_usd and metrics.run_cost_usd(self.last_usage) > max_usd:
                return (
                    f"⚠️ Fix-loop detenido: la corrida supera FIX_LOOP_MAX_USD "
                    f"(${max_usd:.2f}). Revisa manualmente.\n\n{dev_result}"
                )

            # 2. Revisión del candidato: 4R structured (Nivel 5+) sobre el dev_result,
            #    con fallback a pr_review_agent + _review_decision.
            from minka.core.agents.base_agent import build_llm_client
            from minka.core.orchestrator.four_r_review import run_four_r_review

            four_r = run_four_r_review(dev_result, build_llm_client())
            if four_r is not None:
                # La decisión de routing es interna: al dev solo el conteo (detalle con --debug).
                if getattr(config, "DEBUG", False):
                    console.print(
                        Panel(
                            four_r.summary,
                            title="[magenta]Revisión 4R[/magenta]",
                            border_style="magenta",
                        )
                    )
                else:
                    console.print(
                        f"[dim]Revisión 4R: {len(four_r.blockers)} hallazgo(s) bloqueante(s).[/dim]"
                    )
                self.state.record_step("four_r_review", "Revisión 4R del candidato", four_r.summary)
                routing = four_r.routing_decision
                review_result = four_r.summary
                pending = four_r.blockers
            else:
                review_task = f"Revisa el último cambio aplicado. Contexto: {original_task}"
                review_agent = self._get_agent("pr_review_agent")
                review_agent.conversation = base_history
                review_result = review_agent.run(review_task)
                self.state.record_step("pr_review_agent", review_task, review_result)
                self._record_usage(review_agent)
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
            consecutive_fixes += 1
            if consecutive_fixes >= 2:
                return (
                    "⚠️ Dos revisiones seguidas pidieron correcciones — requiere revisión humana.\n\n"
                    "Hallazgos pendientes:\n" + "\n".join(f"- {f}" for f in pending_findings)
                )

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
            from minka.core.agents.base_agent import build_llm_client, resolve_model_id
            from minka.core.structured import call_structured

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
