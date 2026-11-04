"""
dev-agent — punto de entrada.

Conversación natural en terminal con el orquestador multi-agente.

Uso:
    minka                      # modo interactivo (chat con historial)
    minka "analiza los riesgos del repo <alias>"
    minka "analiza X" --agent architecture      # salta el planner
    minka "analiza X" --agent report --json-out /tmp/r.json
    minka --debug              # muestra input/output de cada tool call
    minka --level 3
    minka --index-repos        # Nivel 4 (RAG) — indexa código Go
    minka --index-knowledge    # Nivel 4 (RAG) — indexa .md de knowledge
    minka --init                       # crea config_local.py y .env en el directorio de datos
    minka --new-project mi-proyecto  # crea el esqueleto de un proyecto nuevo
    minka stats [--day YYYY-MM-DD | --month YYYY-MM] [--project P] [--agent A]
    minka evals routing --runs 3 [--json-out r.json]  # solo planner; cuesta tokens
    minka "analiza X" --quiet  # sin la línea final de tokens/costo (stderr)
    minka --help

Comandos del modo interactivo:
    /status  — estado del sistema (sesión AWS, backend, repos, memoria, nivel)
    /clear   — limpia el historial de la sesión actual
    salir    — termina la sesión
"""

import json
import os
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from typer.core import TyperGroup

from minka._app import QUIET_ENV, SOURCE_ENV
from minka.config_loader import config
from minka.core import metrics
from minka.core.orchestrator import router


class _TaskOrCommandGroup(TyperGroup):
    """`minka stats` es un subcomando; `minka "estadísticas de hoy"` es una tarea.

    Click deja que el argumento posicional `task` del callback se coma el primer
    token, así que un subcomando nunca llegaría a despacharse. Si lo que quedó
    en `task` es el nombre de un subcomando, se reinterpreta como tal y el resto
    de tokens pasan a ser sus argumentos (--day, --month...).
    """

    def _first_positional(self, ctx, args: list[str]) -> str | None:
        value_opts = {
            opt
            for param in self.get_params(ctx)
            if getattr(param, "is_flag", None) is False
            for opt in (*param.opts, *param.secondary_opts)
        }
        expect_value = False
        for tok in args:
            if expect_value:
                expect_value = False
                continue
            if tok.startswith("-") and tok != "-":
                expect_value = tok in value_opts
                continue
            return tok
        return None

    def parse_args(self, ctx, args):
        # Con una tarea, las opciones pueden ir después (`minka "x" --agent report`,
        # como las manda el bridge); con un subcomando, lo que sigue es suyo.
        # El parser lee la bandera del contexto, no del comando.
        ctx.allow_interspersed_args = self._first_positional(ctx, args) not in self.commands
        super().parse_args(ctx, args)
        task = ctx.params.get("task")
        if task in self.commands:
            ctx.params["task"] = None
            ctx.args = [*ctx._protected_args, *ctx.args]
            ctx._protected_args = [task]
        return ctx.args


app = typer.Typer(
    cls=_TaskOrCommandGroup,
    add_completion=False,
    invoke_without_command=True,
    help="Sistema multi-agente para desarrollo asistido por IA.",
)
console = Console()
# Línea final de métricas: stderr para no mezclarse con el resultado (stdout).
stderr_console = Console(stderr=True)

# Agentes delegables con --agent. Solo lectura a propósito: ninguno pasa por el
# ApprovalGate, así que no se cuelgan esperando confirmación en stdin cuando el
# proceso corre sin terminal (llamado desde otra herramienta).
# development y debug quedan fuera: escriben, y su gate necesita un humano.
READONLY_AGENTS = tuple(a.removesuffix("_agent") for a in router.READONLY_AGENTS)


def _require_llm_backend() -> None:
    """Sale con una línea clara si no hay backend LLM configurado (no un traceback)."""
    from minka.core.agents.base_agent import backend_unavailable_message

    missing = backend_unavailable_message()
    if missing:
        typer.echo(missing, err=True)
        raise typer.Exit(code=1)


def _make_orchestrator():
    """Import perezoso del orquestador (evita cargar anthropic en --help)."""
    from minka.core.orchestrator.orchestrator import Orchestrator

    return Orchestrator(config=config)


def _write_json_out(
    path: Path,
    task: str,
    agent: str | None,
    result: str,
    ok: bool,
    usage: dict | None,
    record: dict | None = None,
) -> bool:
    """Escribe el resultado como JSON para consumo programático.

    Va a un archivo y no a stdout porque stdout lleva la UI de Rich (paneles,
    plan de ejecución) y no es parseable. Devuelve False si no pudo escribir:
    el resultado ya se imprimió, así que un fallo aquí no debe romper el run.
    Las claves de métricas (run_id, duration_s, cost_usd, baseline_usd,
    pricing_version) son adicionales: quien solo lee ok/result/usage no cambia.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = record or {}
        payload = {
            "ok": ok,
            "agent": agent,
            "task": task,
            "result": result,
            "usage": usage or {},
            "run_id": record.get("run_id"),
            "router": record.get("router"),
            "reason": record.get("reason"),
            "confidence": record.get("confidence"),
            "duration_s": record.get("duration_s"),
            "cost_usd": record.get("cost_usd"),
            "baseline_usd": record.get("baseline_usd"),
            "pricing_version": record.get("pricing_version"),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return True
    except OSError as exc:
        console.print(f"[yellow]No se pudo escribir --json-out ({exc}).[/yellow]")
        return False


def _count_go_files(repo_path: str) -> int | None:
    """Archivos .go del repo (excluye vendor/), o None si la ruta no existe."""
    root = Path(repo_path)
    if not root.is_dir():
        return None
    return sum(1 for f in root.rglob("*.go") if "vendor" not in f.parts)


def _print_status(turns: int) -> None:
    """Comando /status — estado del sistema."""
    if getattr(config, "USE_BEDROCK", False):
        token = config.get_aws_session().sso_token_status()
        if token["valid"]:
            aws_line = f"✓ válida ({token['minutes_remaining']} min restantes)"
        else:
            aws_line = f"✗ expirada — aws sso login --profile {config.AWS_PROFILE}"
        from minka.core.agents.base_agent import resolve_model_id

        backend_line = f"Bedrock — {resolve_model_id(config.ANTHROPIC_MODEL)}"
    else:
        aws_line = "n/a (API directa)"
        backend_line = f"API directa — {config.ANTHROPIC_MODEL}"

    repo_parts = []
    for alias, path in config.REPOS.items():
        count = _count_go_files(path)
        repo_parts.append(
            f"✓ {alias} ({count})" if count is not None else f"✗ {alias} (no encontrado)"
        )

    lines = [
        f" Sesión AWS : {aws_line}",
        f" Backend    : {backend_line}",
        f" Repos Go   : {' | '.join(repo_parts)}",
        f" Memoria    : {turns} turnos en sesión",
        f" RAG        : {'activado' if config.RAG_ENABLED else 'desactivado (Nivel 4)'}",
        f" Nivel      : {config.AUTONOMY_LEVEL} — "
        + ("ApprovalGate activo" if config.AUTONOMY_LEVEL < 5 else "auto-aprobación"),
    ]
    console.print(Panel("\n".join(lines), title="Estado del sistema", border_style="cyan"))


def _print_run_summary(orchestrator, quiet: bool) -> None:
    """Línea final de la corrida en stderr (tokens, cache, costo, tiempo, agentes)."""
    if quiet or os.getenv(QUIET_ENV):
        return
    record = getattr(orchestrator, "last_run_record", None)
    if record:
        # soft_wrap: una sola línea, sin cortes por ancho de terminal (parseable).
        stderr_console.print(
            metrics.format_run_summary(record),
            markup=False,
            highlight=False,
            soft_wrap=True,
            style="dim",
        )


@app.callback()
def main(
    ctx: typer.Context,
    task: str = typer.Argument(
        None, help="Tarea a ejecutar (opcional). Sin ella, entra en modo interactivo."
    ),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="Modo conversacional."
    ),
    quiet: bool = typer.Option(
        False, "--quiet", help="Omite las líneas de decisión y de tokens/costo (van a stderr)."
    ),
    read_only: bool = typer.Option(
        False,
        "--read-only",
        help=f"Si el router infiere un agente que escribe, responde y sale con 2. Implícito con {SOURCE_ENV}=bridge.",
    ),
    level: int = typer.Option(None, help="Override del nivel de autonomía (3-6)."),
    project: str = typer.Option(
        None, "--project", help="Proyecto activo (default: DEFAULT_PROJECT de config_local)."
    ),
    debug: bool = typer.Option(
        False, "--debug", help="Muestra input y resultado de cada tool call."
    ),
    completo: bool = typer.Option(
        False,
        "--completo",
        help="Fuerza la respuesta completa (sin el perfil breve de las consultas puntuales).",
    ),
    index_repos: bool = typer.Option(
        False, "--index-repos", help="Nivel 4: indexar repos en ChromaDB (stub)."
    ),
    index_knowledge: bool = typer.Option(
        False,
        "--index-knowledge",
        help="Nivel 4: indexar .md de knowledge en ChromaDB (knowledge_docs).",
    ),
    init: bool = typer.Option(
        False,
        "--init",
        help="Crea config_local.py y .env en el directorio de datos (nunca sobrescribe) y sale.",
    ),
    new_project: bool = typer.Option(
        False,
        "--new-project",
        help="Crea el esqueleto de un proyecto nuevo (nombre como argumento o interactivo) y sale.",
    ),
    evaluate: bool = typer.Option(
        False,
        "--evaluate",
        help="Nivel 5: corre el golden set de evals del proyecto (requiere EVALUATOR_ENABLED + LLM).",
    ),
    optimize_prompt: bool = typer.Option(
        False,
        "--optimize-prompt",
        help="Nivel 6: propone un prompt mejorado para un agente (ADR, no auto-aplica). Ej: --optimize-prompt pr_review_agent",
    ),
    push_prompts: bool = typer.Option(
        False,
        "--push-prompts",
        help="Ola 3: sube los prompts del proyecto activo al registro versionado (Langfuse).",
    ),
    no_llm_planner: bool = typer.Option(
        False, "--no-llm-planner", help="Usa routing por keywords en vez de LLM."
    ),
    agent: str = typer.Option(
        None,
        "--agent",
        "-a",
        help=f"Fuerza un agente y salta el planner. Disponibles: {', '.join(READONLY_AGENTS)}.",
    ),
    json_out: Path = typer.Option(
        None,
        "--json-out",
        help="Escribe el resultado como JSON en esta ruta (stdout lleva la UI de Rich).",
    ),
):
    # Subcomando (`minka stats`): no es una corrida, nada de lo de abajo aplica.
    if ctx.invoked_subcommand:
        return

    console.print(
        Panel(
            "[bold blue]dev-agent[/bold blue]\n"
            "[dim]Sistema multi-agente para desarrollo asistido por IA[/dim]",
            border_style="blue",
        )
    )

    if project:
        from minka.config_loader import set_project

        try:
            set_project(project)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
    if getattr(config, "ACTIVE_PROJECT", None):
        console.print(f"[dim]Proyecto activo: {config.ACTIVE_PROJECT}[/dim]")

    if level:
        config.AUTONOMY_LEVEL = level
        console.print(f"[dim]Nivel de autonomía: {level}[/dim]")

    if debug:
        config.DEBUG = True
        console.print("[dim]Modo debug: se muestra input/resultado de cada tool call.[/dim]")

    # Nivel 4 — indexado RAG (stub controlado por flag).
    if index_repos:
        if not config.RAG_ENABLED:
            console.print(
                "[yellow]RAG_ENABLED=False. Activa RAG en config e instala chromadb "
                "antes de indexar. (Nivel 4 — pendiente)[/yellow]"
            )
            raise typer.Exit(code=0)
        from minka.core.tools.rag.indexer import index_repos as _index

        stats = _index(config)
        console.print(stats)
        console.print("[green]Indexado completado.[/green]")
        raise typer.Exit(code=0)

    # Nivel 4 — indexado de conocimiento (.md) en la colección knowledge_docs.
    if index_knowledge:
        if not config.RAG_ENABLED:
            console.print(
                "[yellow]RAG_ENABLED=False. Activa RAG en config e instala chromadb "
                "antes de indexar. (Nivel 4 — pendiente)[/yellow]"
            )
            raise typer.Exit(code=0)
        from minka.core.tools.rag.indexer import index_knowledge as _index_knowledge

        stats = _index_knowledge(config)
        console.print(stats)
        console.print("[green]Indexado de knowledge completado.[/green]")
        raise typer.Exit(code=0)

    # Scaffolding — crea el esqueleto de un proyecto nuevo (Arnés Global).
    # Usa el argumento posicional `task` como nombre; si falta, lo pregunta.
    if init:
        from minka.core.project_scaffold import init_data_dir

        result = init_data_dir()
        console.print(f"Directorio de datos: {result['data_dir']}")
        for path in result["created"]:
            console.print(f"  [green]creado[/green]   {path}")
        for path in result["skipped"]:
            console.print(f"  [yellow]ya existe[/yellow] {path} (no se toca)")
        return

    if new_project:
        from minka.core.project_scaffold import (
            InvalidProjectNameError,
            ProjectExistsError,
            create_project,
        )

        name = (task or "").strip()
        if not name:
            name = console.input(
                "[bold]Nombre del proyecto[/bold] (kebab-case, ej: mi-proyecto): "
            ).strip()
        core_module = (
            console.input(
                "[dim]CORE_MODULE_NAME (módulo core compartido; Enter para dejar placeholder): [/dim]"
            ).strip()
            or None
        )
        try:
            result = create_project(name, core_module=core_module)
        except (ProjectExistsError, InvalidProjectNameError, FileNotFoundError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1)
        console.print(
            Panel(
                f"[green]Proyecto '{result['name']}' creado.[/green]\n"
                f"Proyecto : {result['project_dir']}\n"
                f"Contexto : {result['active_context']}",
                title="Nuevo proyecto",
                border_style="green",
            )
        )
        console.print("[bold]Próximos pasos (rutas de tu máquina):[/bold]")
        for i, step in enumerate(result["next_steps"], 1):
            console.print(f"  {i}. {step}")
        console.print(
            f"\n[dim]La próxima sesión detectará '{result['name']}' automáticamente.[/dim]"
        )
        raise typer.Exit(code=0)

    # Nivel 5 — evals de regresión de calidad (golden set por proyecto).
    if evaluate:
        proj = (task or "").strip() or getattr(config, "ACTIVE_PROJECT", None)
        if not proj:
            console.print(
                f"[red]Especifica el proyecto: {config.APP_NAME} --evaluate <proyecto> "
                "(o usa --project).[/red]"
            )
            raise typer.Exit(code=1)
        _require_llm_backend()
        from minka.core.evaluator.regression_evals import run_evaluation

        report = run_evaluation(proj)
        console.print(report)
        raise typer.Exit(code=0)

    # Nivel 6 — optimización reflexiva de prompts (propone ADR, NO auto-aplica).
    if optimize_prompt:
        agent_name = (task or "").strip()
        if not agent_name:
            console.print(
                f"[red]Especifica el agente: {config.APP_NAME} --optimize-prompt <agente>[/red]"
            )
            raise typer.Exit(code=1)
        proj = getattr(config, "ACTIVE_PROJECT", None)
        if not proj:
            console.print("[red]No hay proyecto activo (usa --project).[/red]")
            raise typer.Exit(code=1)
        try:
            from minka.core.evaluator.prompt_optimizer import PromptOptimizer

            opt = PromptOptimizer(config)
        except NotImplementedError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=0)
        _require_llm_backend()
        result = opt.propose(agent_name, proj)
        if "error" in result:
            console.print(f"[red]{result['error']}[/red]")
            raise typer.Exit(code=0)
        console.print(
            Panel(
                str(result["baseline"]), title=f"Baseline evals — {agent_name}", border_style="cyan"
            )
        )
        console.print(
            Panel(
                result["proposed_prompt"],
                title="Prompt propuesto (ADR — NO aplicado)",
                border_style="green",
            )
        )
        console.print(f"[dim]{result['note']}[/dim]")
        raise typer.Exit(code=0)

    # Ola 3 — sembrar el registro versionado de prompts (Langfuse).
    if push_prompts:
        if not getattr(config, "PROMPT_REGISTRY_ENABLED", False):
            console.print(
                "[yellow]PROMPT_REGISTRY_ENABLED=False. Actívalo y configura Langfuse "
                "(LANGFUSE_*) para versionar prompts.[/yellow]"
            )
            raise typer.Exit(code=0)
        from minka.core.prompt_registry import seed_registry

        console.print(seed_registry())
        raise typer.Exit(code=0)

    if agent and agent not in READONLY_AGENTS:
        console.print(
            f"[red]Agente '{agent}' no delegable. Disponibles: "
            f"{', '.join(READONLY_AGENTS)}.[/red]\n"
            "[dim]development y debug escriben y requieren el ApprovalGate "
            "interactivo — úsalos en el modo conversacional.[/dim]"
        )
        raise typer.Exit(code=2)
    if agent and not task:
        console.print('[red]--agent requiere una tarea: minka "tarea" --agent <nombre>[/red]')
        raise typer.Exit(code=2)

    _require_llm_backend()
    orchestrator = _make_orchestrator()
    orchestrator.source = "cli" if task else "repl"
    orchestrator.quiet = quiet
    orchestrator.read_only = read_only or os.getenv(SOURCE_ENV) == "bridge"
    # --completo y --debug apagan el perfil breve (ver core/orchestrator/response_profile.py).
    orchestrator.force_full = completo or debug
    # Flags de runtime que leen los agentes (avisos en stderr, progreso en TTY).
    config.QUIET = quiet
    config.PROGRESS_ENABLED = json_out is None

    if task:
        try:
            result = orchestrator.run(
                task,
                use_llm_planner=not no_llm_planner,
                forced_agent=agent,
                # Con agente forzado se salta el planner: reanudar un run previo
                # no aplica. Sin --agent se conserva el comportamiento de siempre.
                resume=agent is None,
            )
        except KeyboardInterrupt:
            console.print("\nSesión terminada. Hasta luego.")
            raise typer.Exit(code=130)
        # Texto del agente tal cual: sin marcado de Rich (no se come los [...]).
        console.print(result, markup=False)
        _print_run_summary(orchestrator, quiet)
        # last_run_ok distingue "corrió y respondió" de "no pudo correr"
        # (p.ej. sesión AWS expirada, que devuelve un string, no una excepción).
        ok = getattr(orchestrator, "last_run_ok", True)
        if json_out:
            _write_json_out(
                json_out,
                task,
                agent,
                result,
                ok,
                getattr(orchestrator, "last_usage", None),
                getattr(orchestrator, "last_run_record", None),
            )
        if not ok:
            raise typer.Exit(code=getattr(orchestrator, "last_exit_code", None) or 1)
        return

    if interactive:
        console.print(
            "[dim]Escribe tu pedido en español. Comandos: /status, /clear, salir.[/dim]\n"
        )
        try:
            while True:
                user_input = console.input("[bold green]>[/bold green] ").strip()
                if not user_input:
                    continue
                low = user_input.lower()
                if low in ("salir", "exit", "quit"):
                    break
                if low == "/status":
                    _print_status(len(getattr(orchestrator, "memory", [])))
                    continue
                if low == "/clear":
                    orchestrator.clear_session()
                    console.print("[dim]Historial limpio.[/dim]")
                    continue
                if low.startswith("/"):
                    console.print(
                        "[yellow]Comando desconocido. Disponibles: /status, /clear, salir.[/yellow]"
                    )
                    continue
                # El historial vive en orchestrator.memory: aquí solo viaja el pedido crudo.
                result = orchestrator.run(user_input, use_llm_planner=not no_llm_planner)
                console.print(f"\n{result}\n", markup=False)
                _print_run_summary(orchestrator, quiet)
        except (KeyboardInterrupt, EOFError):
            pass
        console.print("\nSesión terminada. Hasta luego.")


@app.command("stats", help="Tokens, cache, costo y tiempo de las corridas registradas.")
def stats(
    day: str = typer.Option(None, "--day", help="Día local YYYY-MM-DD (default: hoy)."),
    month: str = typer.Option(None, "--month", help="Mes local YYYY-MM (agrega sus días)."),
    project: str = typer.Option(None, "--project", help="Solo corridas de este proyecto."),
    agent: str = typer.Option(None, "--agent", help="Solo este agente (p.ej. development)."),
    source: str = typer.Option(
        None, "--source", help=f"Solo corridas con este origen (repl|cli|bridge o la etiqueta {SOURCE_ENV})."
    ),
):
    label = f"mes {month}" if month else f"día {day or metrics.local_day()}"
    if source:
        label += f" · source {source}"
    agg = metrics.aggregate(metrics.read_run_records(day=day, month=month), project, agent, source)
    if not agg["runs"]:
        console.print(f"Sin corridas registradas para {label}.")
        return

    tokens = agg["tokens"]
    total = sum(
        tokens[k]
        for k in (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
    )
    saving = ""
    if agg["baseline_usd"]:
        pct = (agg["baseline_usd"] - agg["cost_usd"]) / agg["baseline_usd"]
        saving = f" (baseline opus {metrics.fmt_usd(agg['baseline_usd'])}, ahorro {pct:.0%})"
    console.print(
        f"[bold]{label}[/bold] · corridas {agg['runs']} · ok {agg['ok']} · "
        f"{agg['duration_s'] / 60:.0f} min · {metrics.fmt_tokens(total)} tokens "
        f"({metrics.fmt_cache(tokens)}) · {metrics.fmt_usd(agg['cost_usd'])}{saving} "
        f"{metrics.SCOPE_NOTE}",
        soft_wrap=True,
    )

    table = Table(show_edge=False, pad_edge=False)
    for col in ("agente", "corridas", "requests", "tokens", "cache", "$", "p90 $"):
        table.add_column(col, justify="left" if col == "agente" else "right")
    for name, row in sorted(agg["agents"].items(), key=lambda kv: -kv[1]["cost_usd"]):
        row_total = sum(
            row[k]
            for k in (
                "input_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "output_tokens",
            )
        )
        table.add_row(
            name,
            str(row["runs"]),
            str(row["requests"]),
            metrics.fmt_tokens(row_total),
            metrics.fmt_cache(row).removeprefix("cache "),
            metrics.fmt_usd(row["cost_usd"]),
            metrics.fmt_usd(row["p90_usd"]),
        )
    console.print(table)

    routers = " · ".join(f"{k} {v}" for k, v in sorted(agg["routers"].items()))
    console.print(
        f"[dim]router: {routers}     fix-loop: {agg['fix_loop_runs']} corrida(s), "
        f"{agg['fix_loop_iters']} iteraciones     tools repetidas bloqueadas: "
        f"{agg['repeated_tool_calls']}     cierres recortados: {agg['closings_removed']} "
        f"({agg['ends_with_offer']} respuestas con oferta)[/dim]"
    )


evals_app = typer.Typer(help="Evaluaciones del motor que llaman al LLM (cuestan tokens).")
app.add_typer(evals_app, name="evals")


@evals_app.command("routing", help="Mide el planner con los casos de información (solo planner, sin agentes).")
def evals_routing(
    runs: int = typer.Option(3, "--runs", min=1, max=5, help="Corridas por caso (máx 5)."),
    json_out: Path = typer.Option(None, "--json-out", help="Escribe el reporte completo en JSON."),
):
    from minka.core.evaluator import routing_evals

    _require_llm_backend()
    if getattr(config, "USE_BEDROCK", False):
        ok, msg = config.get_aws_session().ensure_sso_login()
        if not ok:
            console.print(msg, markup=False)
            raise typer.Exit(code=1)
    cases = routing_evals.info_cases()
    console.print(
        f"[dim]{len(cases)} casos × {runs} corridas, solo el planner "
        f"({config.MODELS.get('planner', '?')}); nunca agentes.[/dim]"
    )
    report = routing_evals.evaluate(cases, runs)
    console.print(routing_evals.format_report(report), markup=False, soft_wrap=True)
    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        console.print(f"[dim]Reporte: {json_out}[/dim]")


knowledge_app = typer.Typer(help="Mantenimiento del knowledge del proyecto (sin LLM).")
app.add_typer(knowledge_app, name="knowledge")


@knowledge_app.command(
    "check",
    help="Verifica las citas archivo:línea de knowledge/extracted/ y del catálogo de riesgos "
    "contra los repos de REPOS. Exit 1 si hay citas rotas.",
)
def knowledge_check(
    project: str = typer.Option(
        None, "--project", help="Proyecto a revisar (default: el activo / DEFAULT_PROJECT)."
    ),
):
    from minka.core.tools.analysis import citations

    if project:
        from minka.config_loader import set_project

        try:
            set_project(project)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
    active = getattr(config, "ACTIVE_PROJECT", None)
    if not active:
        console.print("[red]No hay proyecto activo: usa --project o DEFAULT_PROJECT.[/red]")
        raise typer.Exit(code=2)
    index = citations.RepoIndex(getattr(config, "REPOS", {}))
    if index.empty:
        console.print("[red]REPOS no tiene rutas válidas: nada contra qué verificar.[/red]")
        raise typer.Exit(code=2)
    results = citations.check_project(config, active)
    console.print(citations.format_report(results), markup=False, soft_wrap=True)
    if citations.broken(results):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
