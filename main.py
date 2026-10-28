"""
dev-agent — punto de entrada.

Conversación natural en terminal con el orquestador multi-agente.

Uso:
    python main.py                      # modo interactivo (chat con historial)
    python main.py "analiza los riesgos del repo <alias>"
    python main.py "analiza X" --agent architecture      # salta el planner
    python main.py "analiza X" --agent report --json-out /tmp/r.json
    python main.py --debug              # muestra input/output de cada tool call
    python main.py --level 3
    python main.py --index-repos        # Nivel 4 (RAG) — indexa código Go
    python main.py --index-knowledge    # Nivel 4 (RAG) — indexa .md de knowledge
    python main.py --new-project mi-proyecto  # crea el esqueleto de un proyecto nuevo
    python main.py --help

Comandos del modo interactivo:
    /status  — estado del sistema (sesión AWS, backend, repos, memoria, nivel)
    /clear   — limpia el historial de la sesión actual
    salir    — termina la sesión
"""

import json
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from config_loader import config

load_dotenv()  # carga ANTHROPIC_API_KEY desde .env si existe

app = typer.Typer(
    add_completion=False, help="Sistema multi-agente para desarrollo asistido por IA."
)
console = Console()

# Historial de sesión: cuántos turnos recientes se inyectan como contexto y
# cuánto se trunca cada respuesta (mantiene el contexto acotado por turno).
HISTORY_MAX_TURNS = 10
HISTORY_RESULT_CHARS = 1500

# Agentes delegables con --agent. Solo lectura a propósito: ninguno pasa por el
# ApprovalGate, así que no se cuelgan esperando confirmación en stdin cuando el
# proceso corre sin terminal (llamado desde otra herramienta).
# development y debug quedan fuera: escriben, y su gate necesita un humano.
READONLY_AGENTS = ("architecture", "report", "pr_review")


def _make_orchestrator():
    """Import perezoso del orquestador (evita cargar anthropic en --help)."""
    from core.orchestrator.orchestrator import Orchestrator

    return Orchestrator(config=config)


def _history_context(history: list[tuple[str, str]]) -> str:
    """Convierte el historial en un bloque de contexto para el orchestrator."""
    if not history:
        return ""
    parts = ["Historial de esta sesión (contexto de turnos previos):"]
    for i, (question, answer) in enumerate(history[-HISTORY_MAX_TURNS:], 1):
        parts.append(f"\n[Turno {i}] Dev: {question}")
        parts.append(f"[Turno {i}] Agente: {answer[:HISTORY_RESULT_CHARS]}")
    parts.append("\n---\n\nPedido actual del dev:")
    return "\n".join(parts)


def _write_json_out(
    path: Path, task: str, agent: str | None, result: str, ok: bool, usage: dict | None
) -> bool:
    """Escribe el resultado como JSON para consumo programático.

    Va a un archivo y no a stdout porque stdout lleva la UI de Rich (paneles,
    plan de ejecución) y no es parseable. Devuelve False si no pudo escribir:
    el resultado ya se imprimió, así que un fallo aquí no debe romper el run.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ok": ok,
            "agent": agent,
            "task": task,
            "result": result,
            "usage": usage or {},
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


def _print_status(history: list[tuple[str, str]]) -> None:
    """Comando /status — estado del sistema."""
    if getattr(config, "USE_BEDROCK", False):
        token = config.get_aws_session().sso_token_status()
        if token["valid"]:
            aws_line = f"✓ válida ({token['minutes_remaining']} min restantes)"
        else:
            aws_line = f"✗ expirada — aws sso login --profile {config.AWS_PROFILE}"
        from core.agents.base_agent import resolve_model_id

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
        f" Memoria    : {len(history)} intercambios en sesión",
        f" RAG        : {'activado' if config.RAG_ENABLED else 'desactivado (Nivel 4)'}",
        f" Nivel      : {config.AUTONOMY_LEVEL} — "
        + ("ApprovalGate activo" if config.AUTONOMY_LEVEL < 5 else "auto-aprobación"),
    ]
    console.print(Panel("\n".join(lines), title="Estado del sistema", border_style="cyan"))


@app.command()
def main(
    task: str = typer.Argument(
        None, help="Tarea a ejecutar (opcional). Sin ella, entra en modo interactivo."
    ),
    interactive: bool = typer.Option(
        True, "--interactive/--no-interactive", help="Modo conversacional."
    ),
    level: int = typer.Option(None, help="Override del nivel de autonomía (3-6)."),
    project: str = typer.Option(
        None, "--project", help="Proyecto activo (default: DEFAULT_PROJECT de config_local)."
    ),
    debug: bool = typer.Option(
        False, "--debug", help="Muestra input y resultado de cada tool call."
    ),
    index_repos: bool = typer.Option(
        False, "--index-repos", help="Nivel 4: indexar repos en ChromaDB (stub)."
    ),
    index_knowledge: bool = typer.Option(
        False,
        "--index-knowledge",
        help="Nivel 4: indexar .md de knowledge en ChromaDB (knowledge_docs).",
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
    console.print(
        Panel(
            "[bold blue]dev-agent[/bold blue]\n"
            "[dim]Sistema multi-agente para desarrollo asistido por IA[/dim]",
            border_style="blue",
        )
    )

    if project:
        from config_loader import set_project

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
        from core.tools.rag.indexer import index_repos as _index

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
        from core.tools.rag.indexer import index_knowledge as _index_knowledge

        stats = _index_knowledge(config)
        console.print(stats)
        console.print("[green]Indexado de knowledge completado.[/green]")
        raise typer.Exit(code=0)

    # Scaffolding — crea el esqueleto de un proyecto nuevo (Arnés Global).
    # Usa el argumento posicional `task` como nombre; si falta, lo pregunta.
    if new_project:
        from core.project_scaffold import (
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
                "[red]Especifica el proyecto: ./agent --evaluate <proyecto> "
                "(o usa --project).[/red]"
            )
            raise typer.Exit(code=1)
        from core.evaluator.regression_evals import run_evaluation

        report = run_evaluation(proj)
        console.print(report)
        raise typer.Exit(code=0)

    # Nivel 6 — optimización reflexiva de prompts (propone ADR, NO auto-aplica).
    if optimize_prompt:
        agent_name = (task or "").strip()
        if not agent_name:
            console.print("[red]Especifica el agente: ./agent --optimize-prompt <agente>[/red]")
            raise typer.Exit(code=1)
        proj = getattr(config, "ACTIVE_PROJECT", None)
        if not proj:
            console.print("[red]No hay proyecto activo (usa --project).[/red]")
            raise typer.Exit(code=1)
        try:
            from core.evaluator.prompt_optimizer import PromptOptimizer

            opt = PromptOptimizer(config)
        except NotImplementedError as exc:
            console.print(f"[yellow]{exc}[/yellow]")
            raise typer.Exit(code=0)
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
        from core.prompt_registry import seed_registry

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
        console.print('[red]--agent requiere una tarea: main.py "tarea" --agent <nombre>[/red]')
        raise typer.Exit(code=2)

    orchestrator = _make_orchestrator()

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
        console.print(result)
        # last_run_ok distingue "corrió y respondió" de "no pudo correr"
        # (p.ej. sesión AWS expirada, que devuelve un string, no una excepción).
        ok = getattr(orchestrator, "last_run_ok", True)
        if json_out:
            _write_json_out(
                json_out, task, agent, result, ok, getattr(orchestrator, "last_usage", None)
            )
        if not ok:
            raise typer.Exit(code=1)
        return

    if interactive:
        history: list[tuple[str, str]] = []
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
                    _print_status(history)
                    continue
                if low == "/clear":
                    history.clear()
                    console.print("[dim]Historial limpio.[/dim]")
                    continue
                if low.startswith("/"):
                    console.print(
                        "[yellow]Comando desconocido. Disponibles: /status, /clear, salir.[/yellow]"
                    )
                    continue
                task_text = f"{_history_context(history)}\n{user_input}" if history else user_input
                result = orchestrator.run(task_text, use_llm_planner=not no_llm_planner)
                history.append((user_input, result))
                console.print(f"\n{result}\n")
        except (KeyboardInterrupt, EOFError):
            pass
        console.print("\nSesión terminada. Hasta luego.")


if __name__ == "__main__":
    app()
