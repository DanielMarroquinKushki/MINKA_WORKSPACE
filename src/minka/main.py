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
    minka init                 # onboarding guiado: DATA_DIR, config_local.py, .env, REPOS y backend
    minka doctor               # diagnóstico local (Python, uv, SSO, REPOS, chromadb, proyecto)
    minka --init                       # solo copia las plantillas (sin preguntas)
    minka --new-project mi-proyecto  # crea el esqueleto de un proyecto nuevo
    minka knowledge check --project P  # verifica las citas archivo:línea del knowledge
    minka stats [--day YYYY-MM-DD | --month YYYY-MM] [--project P] [--agent A]
    minka evals routing --runs 3 [--json-out r.json]  # solo planner; cuesta tokens
    minka "analiza X" --quiet  # sin la línea final de tokens/costo (stderr)
    minka "analiza X" --plain  # respuesta sin renderizar Markdown (también MINKA_PLAIN=1;
                               # sin TTY, --json-out, --quiet y bridge ya salen crudas)
    minka "analiza X" --completo  # sin el perfil breve de las consultas puntuales
    minka --help

Comandos del modo interactivo:
    /status  — estado del sistema (sesión AWS, backend, repos, memoria, nivel)
    /clear   — limpia el historial de la sesión actual
    salir    — termina la sesión
    Enter envía; Alt+Enter inserta un salto de línea. Un pegado de varias líneas se
    muestra colapsado ("Pegado #N +M líneas") y se expande al enviarlo (solo con TTY).
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
from minka.core import metrics, render
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


def _make_reader():
    """Lector del REPL: prompt_toolkit solo con TTY; sin TTY, el console.input de siempre."""
    from minka.core.repl_input import ReplReader

    return ReplReader(
        console,
        tty=_is_tty(),
        collapse_lines=config.PASTE_COLLAPSE_LINES,
        collapse_chars=config.PASTE_COLLAPSE_CHARS,
        warn_tokens=config.PASTE_WARN_TOKENS,
    )


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


def _is_tty() -> bool:
    """Hay un humano al otro lado (stdin y stdout son terminal)."""
    import sys

    return bool(sys.stdin.isatty() and sys.stdout.isatty())


def _activate_project(
    explicit: str | None, *, debug: bool = False, quiet: bool = False, required: bool = True
) -> str | None:
    """Fija el proyecto activo (--project > cwd > DEFAULT_PROJECT) y anuncia su origen.

    Empate: pregunta en TTY, falla con la lista sin TTY (exit 2). Sin proyecto y `required`:
    exit 2 con la pista de --project / --new-project. Devuelve el nombre o None."""
    from minka.config_loader import set_project
    from minka.core import project_resolver

    decision = project_resolver.decide(explicit)
    if debug:
        for note in decision.notices:
            stderr_console.print(f"[dim]aviso: {note}[/dim]")
    if decision.ambiguous:
        if _is_tty():
            console.print("[bold]Este repo pertenece a varios proyectos:[/bold]")
            for i, name in enumerate(decision.candidates, 1):
                console.print(f"  {i}. {name}")
            answer = console.input("¿Cuál uso? (número o nombre): ").strip()
            picked = None
            if answer.isdigit() and 1 <= int(answer) <= len(decision.candidates):
                picked = decision.candidates[int(answer) - 1]
            elif answer in decision.candidates:
                picked = answer
            if not picked:
                console.print("[red]Sin elección: usa --project <nombre>.[/red]")
                raise typer.Exit(code=2)
            decision = project_resolver.Decision(name=picked, origin="elección")
        else:
            console.print(
                "[red]Varios proyectos contienen este directorio: "
                f"{', '.join(decision.candidates)}. Indica --project <nombre> "
                "o fija DEFAULT_PROJECT en config_local.py.[/red]"
            )
            raise typer.Exit(code=2)
    if decision.error:
        if required:
            console.print(f"[red]{decision.error}[/red]")
            raise typer.Exit(code=2)
        return None
    if decision.name and decision.name != getattr(config, "ACTIVE_PROJECT", None):
        try:
            set_project(decision.name)
        except FileNotFoundError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2)
    config.PROJECT_ORIGIN = decision.origin
    if not quiet and not os.getenv(QUIET_ENV):
        console.print(f"[dim]{decision.label()}[/dim]")
    return decision.name


def _require_rag_enabled() -> None:
    """`--index-repos` / `--index-knowledge` sin RAG: mensaje claro y exit 1, nunca 'ok'."""
    if getattr(config, "RAG_ENABLED", False):
        return
    project = getattr(config, "ACTIVE_PROJECT", None)
    where = f"projects/{project}/config.py" if project else "config_local.py"
    console.print(
        f"[red]RAG desactivado: no se indexó nada.[/red] Pon [bold]RAG_ENABLED = True[/bold] en "
        f"{where} (o en config_local.py) e instala el extra [rag] "
        "(`uv tool install 'minka-workspace[rag] @ git+<url>'`); luego repite el comando."
    )
    raise typer.Exit(code=1)


def _parse_repo_flags(flags: list[str]) -> dict[str, str]:
    repos: dict[str, str] = {}
    for item in flags:
        alias, sep, path = item.partition("=")
        if not sep or not alias.strip() or not path.strip():
            console.print(f"[red]--repo espera alias=ruta, recibí: {item!r}[/red]")
            raise typer.Exit(code=2)
        repos[alias.strip()] = path.strip()
    return repos


def _ask_repos_interactive() -> dict[str, str]:
    console.print(
        "\n[bold]Repos del proyecto[/bold] (alias y ruta absoluta; Enter en el alias para terminar)"
    )
    repos: dict[str, str] = {}
    while True:
        alias = console.input("  alias: ").strip()
        if not alias:
            if repos:
                return repos
            console.print("  [yellow]Hace falta al menos un repo.[/yellow]")
            continue
        while True:
            path = console.input(f"  ruta de {alias}: ").strip()
            if not path:
                break
            if Path(path).expanduser().is_dir():
                repos[alias] = path
                break
            console.print("  [yellow]esa ruta no existe; vuelve a escribirla (Enter para cancelar el alias).[/yellow]")


def _new_project_flow(
    name: str,
    stack: str | None,
    repo_flags: list[str],
    compliance_flags: list[str],
    yes: bool,
    rag: bool | None = None,
) -> None:
    """`minka --new-project`: nombre, repos, STACK (detectado del primer repo), COMPLIANCE,
    creación, doctor del proyecto y siguientes pasos. Nunca sobrescribe un proyecto."""
    from minka.core import doctor
    from minka.core.project_scaffold import (
        STACKS,
        InvalidProjectNameError,
        ProjectExistsError,
        create_project,
        detect_stack,
    )

    if not name:
        if yes:
            console.print("[red]--new-project necesita el nombre del proyecto con --yes.[/red]")
            raise typer.Exit(code=2)
        name = console.input("[bold]Nombre del proyecto[/bold] (kebab-case, ej: mi-proyecto): ").strip()

    projects_dir = Path(getattr(config, "PROJECTS_DIR", Path(config.DATA_DIR) / "projects"))
    if (projects_dir / name).exists():
        console.print(f"[red]El proyecto '{name}' ya existe en {projects_dir / name}. No se toca nada.[/red]")
        raise typer.Exit(code=1)

    repos = _parse_repo_flags(repo_flags)
    for alias, path in repos.items():
        if not Path(path).expanduser().is_dir():
            console.print(f"[red]La ruta del repo '{alias}' no existe. Nada creado.[/red]")
            raise typer.Exit(code=2)
    if not repos and not yes:
        repos = _ask_repos_interactive()

    detected = detect_stack(next(iter(repos.values()))) if repos else None
    if stack is None:
        default = detected or "go"
        if yes:
            stack = default
        else:
            hint = f" (detectado: {detected})" if detected else ""
            answer = console.input(f"\n[bold]STACK[/bold] [go/python/node]{hint} (Enter = {default}): ").strip().lower()
            stack = answer or default
    if stack not in STACKS:
        console.print(f"[red]STACK inválido: '{stack}'. Opciones: {', '.join(STACKS)}.[/red]")
        raise typer.Exit(code=2)

    compliance = [c.strip().lower() for c in compliance_flags if c.strip()]
    if not compliance and not yes:
        answer = console.input("[bold]¿Aplica PCI-DSS?[/bold] [s/N]: ").strip().lower()
        compliance = ["pci"] if answer in ("s", "si", "sí", "y", "yes") else []

    if rag is None:
        if yes:
            rag = True
        else:
            answer = console.input("[bold]¿Activar búsqueda semántica (RAG)?[/bold] [S/n]: ").strip().lower()
            rag = answer not in ("n", "no")

    core_module = None
    if stack == "go" and not yes:
        core_module = (
            console.input(
                "[dim]CORE_MODULE_NAME (módulo core compartido; Enter para dejar placeholder): [/dim]"
            ).strip()
            or None
        )

    try:
        result = create_project(
            name, core_module=core_module, stack=stack, compliance=compliance, repos=repos, rag=rag
        )
    except (ProjectExistsError, InvalidProjectNameError, FileNotFoundError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1)

    console.print(
        Panel(
            f"[green]Proyecto '{result['name']}' creado.[/green]\n"
            f"Proyecto : {result['project_dir']}\n"
            f"Contexto : {result['active_context']}\n"
            f"STACK    : {stack} · COMPLIANCE: {compliance or '[]'} · RAG: {'sí' if rag else 'no'} "
            f"· repos: {len(result['repos'])}"
            + (" (rutas relativas; REPOS_ROOT fijado en config_local.py)" if result["repos_root"] else ""),
            title="Nuevo proyecto",
            border_style="green",
        )
    )

    # Doctor del proyecto recién creado: solo ✓/✗ y qué hacer.
    from minka.config_loader import set_project

    try:
        set_project(result["name"])
        checks = doctor.run_checks(config)
        console.print(doctor.format_report(checks), markup=False, highlight=False)
    except Exception as exc:  # noqa: BLE001 — el doctor es informativo, no bloquea la creación
        console.print(f"[yellow]No se pudo correr el doctor: {exc}[/yellow]")

    console.print("\n[bold]Siguiente paso:[/bold]")
    for i, step in enumerate(result["next_steps"], 1):
        console.print(f"  {i}. {step}", markup=False)
    console.print(f"\n[dim]La próxima sesión detectará '{result['name']}' automáticamente.[/dim]")


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
        # Citas de la respuesta verificadas contra REPOS (solo si hubo alguna).
        if record.get("citations_total"):
            stderr_console.print(
                f"{record['citations_total']} citas verificadas, "
                f"{record.get('citations_missing') or 0} no encontradas",
                markup=False,
                highlight=False,
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
    plain: bool = typer.Option(
        False,
        "--plain",
        help="Imprime la respuesta sin renderizar Markdown (equivale a MINKA_PLAIN=1).",
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
        help="Copia config_local.py y .env al directorio de datos (nunca sobrescribe) y sale. "
        "Para el onboarding guiado usa `minka init`.",
    ),
    new_project: bool = typer.Option(
        False,
        "--new-project",
        help="Crea un proyecto nuevo (nombre como argumento o interactivo): pregunta STACK, "
        "COMPLIANCE y repos, corre el doctor y sale. Con --repo/--stack/--yes no pregunta.",
    ),
    stack: str = typer.Option(
        None, "--stack", help="--new-project: go | python | node (default: detectado del primer repo)."
    ),
    repo: list[str] = typer.Option(
        None, "--repo", help="--new-project: alias=ruta del repo (repetible). La ruta debe existir."
    ),
    compliance: list[str] = typer.Option(
        None, "--compliance", help="--new-project: cumplimiento opcional, p. ej. pci (repetible)."
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="--new-project: sin preguntas; usa flags y defaults."
    ),
    rag: bool = typer.Option(
        None,
        "--rag/--no-rag",
        help="--new-project: activa la búsqueda semántica (RAG_ENABLED) en el proyecto. Default: sí.",
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

    # Proyecto activo: --project > cwd > DEFAULT_PROJECT (core/project_resolver.py). Los
    # comandos de mantenimiento (--init, --new-project) no lo necesitan.
    if not (init or new_project):
        _activate_project(
            project,
            debug=debug,
            quiet=quiet,
            # Estos flags toman el proyecto del argumento o validan por su cuenta.
            required=not (evaluate or optimize_prompt or push_prompts),
        )

    if level:
        config.AUTONOMY_LEVEL = level
        console.print(f"[dim]Nivel de autonomía: {level}[/dim]")

    if debug:
        config.DEBUG = True
        console.print("[dim]Modo debug: se muestra input/resultado de cada tool call.[/dim]")

    # Nivel 4 — indexado RAG (stub controlado por flag).
    if index_repos or index_knowledge:
        _require_rag_enabled()
        from minka.core.tools.rag import indexer as _indexer

        if index_repos:
            stats = _indexer.index_repos(config)
            label = "Indexado de código"
        else:
            stats = _indexer.index_knowledge(config)
            label = "Indexado de knowledge"
        console.print(stats)
        if stats.get("error"):
            console.print(f"[red]{label} no se completó: {stats['error']}[/red]")
            raise typer.Exit(code=1)
        if stats.get("errors"):
            console.print(
                f"[yellow]{label} terminó con {stats['errors']} repo(s)/fuente(s) con error "
                "(ver desglose).[/yellow]"
            )
            raise typer.Exit(code=1)
        console.print(f"[green]{label} completado.[/green]")
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
        _new_project_flow(
            name=(task or "").strip(),
            stack=stack,
            repo_flags=repo or [],
            compliance_flags=compliance or [],
            yes=yes,
            rag=rag,
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
        # Markdown legible solo con TTY y sin --quiet/--json-out/--plain/bridge; si no, el
        # texto crudo sin marcado de Rich (no se come los [...]). Ver core/render.py.
        render.print_result(
            result,
            console,
            render=render.should_render(quiet=quiet, json_out=json_out is not None, plain=plain),
        )
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
        reader = _make_reader()
        keys = " Enter envía; Alt+Enter, salto de línea." if reader.tty else ""
        console.print(
            f"[dim]Escribe tu pedido en español.{keys} Comandos: /status, /clear, salir.[/dim]\n"
        )
        try:
            while True:
                # Texto ya expandido: la memoria y el router nunca ven el marcador del pegado.
                user_input = reader.read()
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
                console.print()
                render.print_result(
                    result, console, render=render.should_render(quiet=quiet, plain=plain)
                )
                console.print()
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
        f"({agg['ends_with_offer']} respuestas con oferta)     citas no encontradas: "
        f"{agg['citations_missing']}/{agg['citations_total']}[/dim]"
    )
    hashes = agg.get("prompt_hashes") or {}
    if hashes:
        listed = " · ".join(f"{h} ×{n}" for h, n in sorted(hashes.items(), key=lambda kv: -kv[1]))
        console.print(
            f"[dim]línea base (prompt_hash): {listed}"
            + ("     ⚠ más de una línea base en el rango" if len(hashes) > 1 else "")
            + "[/dim]",
            soft_wrap=True,
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

    _activate_project(project, quiet=True, required=False)
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


@knowledge_app.command(
    "init",
    help="Genera un borrador de knowledge (architecture.md + _sources.json) a partir del código, "
    "sin modelo: árbol, módulos, imports, README, manifiesto y git log, con citas verificadas. "
    "Escribe en output/knowledge_draft/<p>/ y muestra el diff; --apply lo copia sin sobrescribir.",
)
def knowledge_init(
    project: str = typer.Option(None, "--project", help="Proyecto (default: el activo / por cwd)."),
    apply: bool = typer.Option(False, "--apply", help="Copia el borrador a knowledge/extracted/ (pide confirmación)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Con --apply: no pregunta."),
):
    import difflib

    from minka.core.knowledge import extractor
    from minka.core.tools.analysis import citations

    _activate_project(project, quiet=True, required=True)
    active = getattr(config, "ACTIVE_PROJECT", None)
    try:
        draft = extractor.build_draft(config, active)
    except extractor.UnsupportedStack as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2)
    except extractor.SecretFound as exc:
        console.print(f"[red]{exc}[/red] Revisa y excluye antes de volver a correr:")
        for hit in exc.hits[:50]:
            console.print(f"  {hit}", markup=False)
        if len(exc.hits) > 50:
            console.print(f"  … y {len(exc.hits) - 50} más")
        raise typer.Exit(code=3)

    out_dir = Path(config.OUTPUT_DIR) / "knowledge_draft" / active
    extractor.write_draft(draft, out_dir)
    console.print(draft.report, markup=False)
    console.print(f"Borrador en {out_dir} ({draft.files_scanned} archivos escaneados, 0 secretos).")

    extracted = Path(config.PROJECTS_DIR) / active / "knowledge" / "extracted"
    current = extracted / "architecture.md"
    old = current.read_text(encoding="utf-8", errors="ignore").splitlines() if current.is_file() else []
    diff = list(difflib.unified_diff(old, draft.architecture.splitlines(), "knowledge/extracted/architecture.md", "borrador", lineterm="", n=1))
    if diff:
        added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        console.print(f"[dim]diff vs knowledge/extracted/architecture.md: +{added} −{removed} líneas"
                      + ("" if current.is_file() else " (no existe todavía)") + "[/dim]")
    if not apply:
        console.print("[dim]Revisa el borrador; con --apply se copia a knowledge/extracted/ (nunca sobrescribe).[/dim]")
        return

    if not yes:
        answer = console.input("¿Aplicar el borrador a knowledge/extracted/? [s/N]: ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            console.print("Sin cambios.")
            raise typer.Exit(code=0)
    written = extractor.apply_draft(draft, extracted)
    for name, target in written.items():
        note = " (ya existía: se escribió como .new)" if target.name.endswith(".new") else ""
        console.print(f"  escrito {target}{note}", markup=False)
    results = citations.check_project(config, active)
    console.print(citations.format_report(results), markup=False, soft_wrap=True)
    if citations.broken(results):
        raise typer.Exit(code=1)


@app.command(
    "init",
    help="Onboarding guiado: crea el directorio de datos, config_local.py y .env (sin sobrescribir) "
    "y pregunta REPOS y backend. Nunca imprime los valores que escribes.",
)
def init_cmd(
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Sin preguntas: solo copia las plantillas (igual que --init)."
    ),
):
    from minka.core.project_scaffold import apply_init_answers, init_data_dir

    result = init_data_dir()
    data_dir = Path(result["data_dir"])
    console.print(f"Directorio de datos: {data_dir}")
    for path in result["created"]:
        console.print(f"  [green]creado[/green]   {Path(path).name}")
    for path in result["skipped"]:
        console.print(f"  [yellow]ya existe[/yellow] {Path(path).name} (no se toca)")
    if yes or not result["created"]:
        if not result["created"]:
            console.print("[dim]Nada que preguntar: edita config_local.py y .env a mano.[/dim]")
        console.print("[dim]Siguiente: `minka doctor`.[/dim]")
        return

    console.print("\n[bold]Repos del proyecto[/bold] (alias y ruta absoluta; Enter en el alias para terminar)")
    repos: dict[str, str] = {}
    while True:
        alias = console.input("  alias: ").strip()
        if not alias:
            break
        path = console.input(f"  ruta de {alias}: ").strip()
        if not path:
            continue
        repos[alias] = path
        if not Path(path).expanduser().is_dir():
            console.print("  [yellow]aviso:[/yellow] esa ruta no existe todavía; se guarda igual.")
    backend = console.input("\n[bold]Backend[/bold] [bedrock/anthropic] (Enter = bedrock): ").strip().lower() or "bedrock"
    if backend not in ("bedrock", "anthropic"):
        backend = "bedrock"
    profile = ""
    if backend == "bedrock":
        profile = console.input("Perfil AWS SSO (Enter = bedrock): ").strip() or "bedrock"
    touched = apply_init_answers(data_dir, repos, backend, profile, result["created"])
    console.print(
        f"\n{len(repos)} repo(s) y backend {backend} guardados en {len(touched)} archivo(s)."
    )
    if backend == "anthropic":
        console.print("[dim]Completa ANTHROPIC_API_KEY en .env (no se pide aquí).[/dim]")
    console.print("[dim]Siguiente: `minka doctor` y luego `minka --new-project <nombre>`.[/dim]")


@app.command("doctor", help="Diagnóstico local del entorno: Python, uv, sesión, REPOS, chromadb, proyecto.")
def doctor_cmd(
    project: str = typer.Option(None, "--project", help="Proyecto a verificar (default: el activo)."),
):
    from minka.core import doctor

    # --project > cwd > DEFAULT_PROJECT; sin ninguno el doctor igual corre y lo marca en ✗.
    _activate_project(project, quiet=True, required=False)
    checks = doctor.run_checks(config)
    console.print(doctor.format_report(checks), markup=False, highlight=False)
    if doctor.has_critical_failure(checks):
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
