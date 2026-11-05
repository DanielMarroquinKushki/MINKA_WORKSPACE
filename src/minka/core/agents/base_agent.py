"""
BaseAgent — núcleo de todos los agentes del sistema.

Implementa el patrón ReAct (Reason + Act) con tool-calling nativo de Claude,
memoria de sesión, base de conocimiento inyectada y logging estructurado.

Correcciones frente al prompt original:
- ToolRegistry y KnowledgeLoader ahora existen de verdad (ver esos módulos).
- El modelo se resuelve por agente vía config.MODELS (fallback a ANTHROPIC_MODEL).
- El content del assistant se pasa como lista de bloques (formato correcto de la
  Messages API); el user tool_result también.
- Se registra la sesión en output/logs/.
"""

import json
import os
import sys
from abc import ABC, abstractmethod
from collections import Counter
from datetime import datetime, timezone

import anthropic
from rich.console import Console
from rich.panel import Panel

from minka._app import QUIET_ENV
from minka.config_loader import config
from minka.core import progress
from minka.core.knowledge.loader import KnowledgeLoader
from minka.core.memory.session_memory import SessionMemory
from minka.core.memory.vector_memory import VectorMemory
from minka.core.observability import trace
from minka.core.orchestrator import response_profile
from minka.core.tools.registry import ToolRegistry, resolve_repo_paths

console = Console()
# Avisos al dev (p.ej. tool repetida) sin mezclarse con el resultado en stdout.
stderr_console = Console(stderr=True)


_backend_warned = False


def _warn_if_backend_inferred() -> None:
    """Avisa una vez por proceso si Bedrock se infirió de AWS_PROFILE sin LLM_BACKEND."""
    global _backend_warned
    if getattr(config, "BACKEND_INFERRED", False) and not _backend_warned:
        _backend_warned = True
        print(
            f"⚠️ LLM_BACKEND no está definido: se usa Bedrock porque hay AWS_PROFILE "
            f"({config.AWS_PROFILE}). Define LLM_BACKEND=bedrock o LLM_BACKEND=anthropic "
            "en <DATA_DIR>/.env o en config_local.py.",
            file=sys.stderr,
        )


def backend_unavailable_message() -> str | None:
    """Una línea que explica cómo configurar el backend LLM, o None si hay uno
    utilizable: Bedrock (LLM_BACKEND/AWS_PROFILE) o ANTHROPIC_API_KEY real
    (el placeholder "sk-ant-..." de .env.example no cuenta)."""
    if getattr(config, "USE_BEDROCK", False):
        return None
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if key and not key.endswith("..."):
        return None
    data_dir = config.DATA_DIR
    return (
        f"Sin backend LLM: define LLM_BACKEND/AWS_PROFILE en {data_dir}/config_local.py "
        f"o ANTHROPIC_API_KEY en {data_dir}/.env ({config.APP_NAME} --init crea ambos)."
    )


def build_llm_client():
    """Crea el cliente Anthropic según el backend configurado.

    - Bedrock: usa el perfil SSO (aws_profile/aws_region). La renovación
      automática de credenciales la maneja botocore (Capas 1-2).
    - API directa: anthropic.Anthropic() con ANTHROPIC_API_KEY.

    Módulo-nivel para que otros componentes (planner) usen el mismo backend
    que los agentes sin duplicar la lógica.
    """
    if getattr(config, "USE_BEDROCK", False):
        from minka.aws_session_manager import build_anthropic_bedrock

        _warn_if_backend_inferred()
        # Inicializa el session manager (lazy) para dejar la sesión lista.
        config.get_aws_session()
        return build_anthropic_bedrock(config)
    missing = backend_unavailable_message()
    if missing:
        raise RuntimeError(missing)
    return anthropic.Anthropic()


def resolve_model_id(model_id: str) -> str:
    """Devuelve el model ID efectivo para el backend actual.

    En Bedrock lo normaliza al inference profile regional de forma idempotente:
        'claude-opus-4-8'              → 'us.anthropic.claude-opus-4-8'
        'anthropic.claude-opus-4-8'    → 'us.anthropic.claude-opus-4-8'
        'us.anthropic.claude-opus-4-8' → (sin cambios)
    Quita cualquier prefijo de proveedor conocido y antepone el configurado
    (BEDROCK_MODEL_PREFIX), así nunca se duplica ni se apilan prefijos.
    En API directa devuelve el ID sin tocar."""
    if not getattr(config, "USE_BEDROCK", False):
        return model_id
    prefix = getattr(config, "BEDROCK_MODEL_PREFIX", "us.anthropic.")
    base = model_id
    for known in ("us.anthropic.", "eu.anthropic.", "apac.anthropic.", "anthropic."):
        if base.startswith(known):
            base = base[len(known) :]
            break
    return f"{prefix}{base}"


# Bloque user de la penúltima vuelta (ARCH_MAX_ITERATIONS): respuesta con lo recogido.
FINAL_TURN_BLOCK = (
    "---\n"
    "Última vuelta: responde ahora con lo recogido y conserva todas las citas archivo:línea. "
    "No llames más herramientas."
)
ITERATIONS_EXHAUSTED = "Máximo de iteraciones alcanzado sin respuesta final."
# Argumentos de tools que se registran en usage.per_iteration[].tools: rutas, rangos y
# patrones. Nunca contenido (write_file, etc.) ni lo que la tool devolvió.
TOOL_LOG_ARGS = (
    "path", "start_line", "end_line", "pattern", "glob", "query", "symbol", "name",
    "repo_filter", "n_results", "collection", "max_depth", "include_ext",
)


def _tool_log_entry(name: str, args: dict, blocked: bool = False) -> dict:
    kept = {}
    for key in TOOL_LOG_ARGS:
        if key in args:
            value = args[key]
            kept[key] = value if isinstance(value, (int, float, bool)) else str(value)[:200]
    entry = {"name": name, "args": kept}
    if blocked:
        entry["blocked"] = True
    return entry


class BaseAgent(ABC):
    def __init__(self, name: str):
        self.name = name
        self.config = config
        self.client = self._build_client()
        # SessionMemory queda sin uso: el historial lo pone el orquestador en
        # `self.conversation` (ConversationMemory única, pares user/assistant).
        self.memory = SessionMemory(agent_name=name)
        self.conversation: list[dict] = []
        # Perfil de concisión de UN run (lo fija el orquestador; ver response_profile.py).
        self.response_profile: str | None = None
        # Los fija el orquestador por run: banner "recibió tarea" solo en planes de 2+ pasos;
        # contexto vivo del proyecto como primer bloque del mensaje user (live_context.py).
        self.show_banner: bool = True
        self.live_context_block: str = ""
        self.vector_memory = VectorMemory(
            namespace=getattr(config, "ACTIVE_PROJECT", None) or "global"
        )
        self.tools = ToolRegistry.get_tools_for_agent(name)
        self.knowledge = KnowledgeLoader.load_for_agent(name)
        self.iteration_count = 0
        self.max_iterations = 20  # backstop anti loop infinito
        # Tokens reales del último run(). Se inicializa aquí para que exista
        # incluso si nadie llamó a run() todavía.
        self.last_usage = self._empty_usage()

    def _empty_usage(self) -> dict:
        """Acumulador de tokens en cero para un run nuevo."""
        return {
            "agent": self.name,
            "model": None,
            "requests": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
            # Desglose del write por TTL: confirma cuál se usó de verdad.
            "cache_creation_5m": 0,
            "cache_creation_1h": 0,
            # Llamadas idénticas a una tool bloqueadas por TOOL_REPEAT_LIMIT.
            "repeated_tool_calls": 0,
            # 1 si la corrida llegó a la vuelta tope-1 y se le pidió la respuesta final.
            "iterations_capped": 0,
            # Una entrada por respuesta del modelo: tokens de la vuelta y, si hubo tool_use,
            # `tools` = [{name, args}] con rutas, rangos y patrones (nunca lo que devolvieron).
            "per_iteration": [],
        }

    def _accumulate_usage(self, response) -> None:
        """Suma el usage de una respuesta al acumulado del run.

        Un run del loop ReAct hace N llamadas al LLM (una por iteración), así que
        el costo real es la suma de todas, no la de la última. Best-effort: la
        contabilidad nunca debe romper el loop del agente.
        """
        try:
            usage = getattr(response, "usage", None)
            if usage is None:
                return
            self.last_usage["requests"] += 1
            self.last_usage["model"] = getattr(response, "model", None) or self.last_usage["model"]
            step = {}
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                step[key] = getattr(usage, key, 0) or 0
                self.last_usage[key] += step[key]
            self.last_usage.setdefault("per_iteration", []).append(step)
            # usage.cache_creation desglosa el write por TTL. Puede no venir
            # (backend viejo / sin caching): por eso getattr encadenado.
            breakdown = getattr(usage, "cache_creation", None)
            if breakdown is not None:
                self.last_usage["cache_creation_5m"] += (
                    getattr(breakdown, "ephemeral_5m_input_tokens", 0) or 0
                )
                self.last_usage["cache_creation_1h"] += (
                    getattr(breakdown, "ephemeral_1h_input_tokens", 0) or 0
                )
        except Exception:  # noqa: BLE001 — métricas best-effort
            pass

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Cada agente define su identidad, rol y reglas."""

    # -- Cliente LLM (modo dual: Bedrock vía SSO / API directa) ---------------
    def _build_client(self):
        """Delegado a build_llm_client() (módulo-nivel, compartido con planner)."""
        return build_llm_client()

    # -- Loop ReAct ------------------------------------------------------------
    def run(self, task: str) -> str:
        if getattr(self, "show_banner", True):
            console.print(Panel(f"[bold]{self.name}[/bold] recibió tarea", style="blue"))

        with trace(f"agent:{self.name}", agent=self.name), progress.track(self.name) as prog:
            self._progress = prog
            recalled = self._recall_memories(task)
            user_content = f"{recalled}\n\n{task}" if recalled else task
            # Contexto vivo del proyecto: primer bloque del mensaje user, nunca en el system.
            # Un solo uso por run; no entra en _log, _remember ni en la memoria compartida.
            live = getattr(self, "live_context_block", "") or ""
            self.live_context_block = ""
            if live:
                user_content = f"{live}\n\n---\n\n{user_content}"
            # Perfil breve: bloque al final del mensaje user, nunca en el system (el prefijo
            # cacheado y la línea base de prompts quedan intactos). Un solo uso por run.
            block = response_profile.user_block(getattr(self, "response_profile", None) or "")
            self.response_profile = None
            if block:
                user_content = f"{user_content}\n\n{block}"
            # Copias: el marcador móvil de caché se pone sobre estos dicts, nunca sobre
            # los del historial compartido que el orquestador deja en self.conversation.
            messages = [dict(m) for m in (getattr(self, "conversation", None) or [])]
            messages.append({"role": "user", "content": user_content})
            self.iteration_count = 0
            self.last_usage = self._empty_usage()
            self._tool_calls_seen = Counter()
            self._cached_block = None

            # Se construye UNA vez, fuera del loop: el prefijo cacheado debe ser
            # byte-idéntico en cada iteración o el cache no acierta.
            system = self._system_param()

            while self.iteration_count < self.max_iterations:
                self.iteration_count += 1
                prog.update(iteration=self.iteration_count, status="esperando al modelo")
                self._mark_cache_tail(messages)

                response = self.client.messages.create(
                    model=self._model(),
                    max_tokens=self.config.MAX_TOKENS,
                    system=system,
                    tools=self.tools,
                    messages=messages,
                )
                self._accumulate_usage(response)
                usage = getattr(response, "usage", None)
                prog.update(
                    tokens_in=getattr(usage, "input_tokens", None),
                    tokens_out=getattr(usage, "output_tokens", None),
                )

                if response.stop_reason == "tool_use":
                    tool_results = self._execute_tools(response)
                    if self.iteration_count == self.max_iterations - 1:
                        # Penúltima vuelta: la siguiente debe ser la respuesta, con lo recogido.
                        tool_results.append({"type": "text", "text": FINAL_TURN_BLOCK})
                        self.last_usage["iterations_capped"] = 1
                        self._warn_iteration_cap()
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                    continue

                # end_turn u otro → respuesta final
                result = self._extract_text(response)
                self._remember(task, result)
                self._log(task, result)
                return result

            # Agotó las vueltas pidiendo tools: se entrega el texto que acompañara a la última.
            result = self._extract_text(response) or ITERATIONS_EXHAUSTED
            self._log(task, result)
            return result

    def _warn_iteration_cap(self) -> None:
        if getattr(self.config, "QUIET", False) or os.getenv(QUIET_ENV):
            return
        stderr_console.print(
            f"⚠ {self.name}: vuelta {self.iteration_count} de {self.max_iterations}; "
            "pido la respuesta final con lo recogido.",
            markup=False,
            highlight=False,
            style="yellow",
        )

    # -- Helpers ---------------------------------------------------------------
    def _recall_memories(self, task: str) -> str:
        """Recuerdos de largo plazo relevantes (mem0) como contexto. '' si off/none."""
        mems = self.vector_memory.search(task, limit=3)
        lines = [(m.get("memory", "") if isinstance(m, dict) else str(m)) for m in mems]
        lines = [x for x in lines if x]
        if not lines:
            return ""
        return "Recuerdos relevantes de sesiones anteriores:\n" + "\n".join(
            f"- {x}" for x in lines
        )

    def _remember(self, task: str, result: str) -> None:
        """Guarda el intercambio como recuerdo de largo plazo (mem0). No-op si off."""
        self.vector_memory.add(f"Tarea: {task}\nResultado: {result[:1000]}")

    def _model(self) -> str:
        """Modelo para este agente (config.MODELS[self.name] → DEFAULT_MODEL)."""
        models = getattr(self.config, "MODELS", {})
        default = getattr(self.config, "DEFAULT_MODEL", self.config.ANTHROPIC_MODEL)
        return resolve_model_id(models.get(self.name, default))

    def _build_system_prompt(self) -> str:
        return f"{self.get_system_prompt()}\n\n---\n\n{self.knowledge.get_relevant_context()}"

    def _system_param(self) -> str | list[dict]:
        """El system como lo espera la Messages API, con prompt caching.

        El loop ReAct reenvía identidad + knowledge base en CADA iteración: sin
        cache se paga el prefijo completo N veces (medido: 68% del costo de un
        run del architecture_agent). Marcar el bloque con cache_control hace que
        la 1a llamada lo escriba (1.25x) y las N-1 restantes lo lean (0.10x).

        El breakpoint va al final del system porque el prefijo cacheable es, en
        orden: tools -> system -> messages. Así una sola marca cubre también los
        schemas de las tools, que son parte del prefijo.

        Con PROMPT_CACHE_ENABLED=False devuelve el string plano (comportamiento
        anterior), para poder desactivarlo desde config_local.py sin tocar código.
        """
        text = self._build_system_prompt()
        if not getattr(self.config, "PROMPT_CACHE_ENABLED", True):
            return text
        cache_control = {"type": "ephemeral"}
        ttl = self._cache_ttl()
        # "5m" es el default de la API: no se manda, así el payload de los agentes
        # sin override queda byte-idéntico al de antes de este cambio.
        if ttl != "5m":
            cache_control["ttl"] = ttl
        return [{"type": "text", "text": text, "cache_control": cache_control}]

    def _mark_cache_tail(self, messages: list[dict]) -> None:
        """Marcador móvil: cache_control (TTL 5m) en el último bloque del último mensaje
        `user`, quitando el de la vuelta anterior. Así cada vuelta lee del caché todo el
        historial previo y solo escribe el delta (tool_use + tool_result nuevos).

        Siempre 5m aunque el system vaya a 1h: las vueltas van segundos aparte y la API
        exige que las entradas de 1h precedan a las de 5m (system → mensajes cumple).
        Nunca se marcan bloques `assistant` (objetos del SDK). Con PROMPT_CACHE_ENABLED=False
        no marca nada. No cambia el texto que ve el modelo.
        """
        previous = getattr(self, "_cached_block", None)
        if isinstance(previous, dict):
            previous.pop("cache_control", None)
        self._cached_block = None
        if not getattr(self.config, "PROMPT_CACHE_ENABLED", True) or not messages:
            return
        last = messages[-1]
        if last.get("role") != "user":
            return
        content = last.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
            last["content"] = content
        if not isinstance(content, list) or not content or not isinstance(content[-1], dict):
            return
        content[-1]["cache_control"] = {"type": "ephemeral"}
        self._cached_block = content[-1]

    def _cache_ttl(self) -> str:
        """TTL del cache para este agente (CACHE_TTL_BY_AGENT → CACHE_TTL_DEFAULT).

        Solo "5m" y "1h" son válidos para la API; cualquier otro valor cae a "5m"
        con aviso visible — un typo en config no debe romper todas las corridas,
        pero tampoco debe pasar en silencio haciéndonos creer que el 1h está on.
        """
        by_agent = getattr(self.config, "CACHE_TTL_BY_AGENT", {}) or {}
        ttl = by_agent.get(self.name) or getattr(self.config, "CACHE_TTL_DEFAULT", "5m")
        if ttl not in ("5m", "1h"):
            console.print(f"[yellow]CACHE_TTL inválido ({ttl!r}) para {self.name}; uso 5m.[/yellow]")
            return "5m"
        return ttl

    def _execute_tools(self, response) -> list:
        results = []
        logged = []
        seen = getattr(self, "_tool_calls_seen", None)
        if seen is None:
            seen = self._tool_calls_seen = Counter()
        limit = int(getattr(self.config, "TOOL_REPEAT_LIMIT", 3) or 0)
        prog = getattr(self, "_progress", None)
        for block in response.content:
            if block.type != "tool_use":
                continue
            # La línea "→ Tool" va a stderr y nunca con el progreso vivo (que ya muestra la
            # tool en curso) ni con --quiet: la respuesta en stdout queda limpia.
            if not progress.enabled() and not (
                os.getenv(QUIET_ENV) or getattr(self.config, "QUIET", False)
            ):
                stderr_console.print(f"  → Tool: {block.name}", style="dim", markup=False)
            if prog is not None:
                prog.update(status=f"tool {block.name}")
            # Tope de llamadas idénticas (misma tool, mismos argumentos) por corrida:
            # corta los loops en que el modelo repite la misma lectura.
            key = (block.name, json.dumps(dict(block.input), sort_keys=True, default=str))
            seen[key] += 1
            logged.append(_tool_log_entry(block.name, dict(block.input), blocked=limit and seen[key] > limit))
            if limit and seen[key] > limit:
                self.last_usage["repeated_tool_calls"] = (
                    self.last_usage.get("repeated_tool_calls", 0) + 1
                )
                msg = (
                    f"Llamada repetida: {block.name} con los mismos argumentos ya se ejecutó "
                    f"{limit} veces en esta corrida. Usa el resultado anterior."
                )
                if not (getattr(self.config, "QUIET", False) or os.getenv(QUIET_ENV)):
                    stderr_console.print(
                        f"⚠ {self.name}: {msg}",
                        markup=False,
                        highlight=False,
                        soft_wrap=True,
                        style="yellow",
                    )
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"ERROR: {msg}",
                        "is_error": True,
                    }
                )
                continue
            if getattr(self.config, "DEBUG", False):
                console.print(
                    f"    [dim]input:[/dim] {json.dumps(dict(block.input), ensure_ascii=False)[:600]}"
                )
            try:
                tool_fn = ToolRegistry.get(block.name)
                kwargs = dict(block.input)
                # Inyección de config para tools que la necesitan.
                # También si el LLM mandó un dict vacío (falso negativo de drift).
                if block.name == "check_version_drift" and not kwargs.get("repos"):
                    kwargs["repos"] = self.config.REPOS
                # Alias de REPOS → ruta real ANTES de la tool (y del ApprovalGate).
                kwargs, path_error = resolve_repo_paths(
                    block.name, kwargs, getattr(self.config, "REPOS", {})
                )
                output = path_error or tool_fn(**kwargs)
                if getattr(self.config, "DEBUG", False):
                    console.print(f"    [dim]result:[/dim] {str(output)[:600]}")
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(output),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": f"ERROR: {exc}",
                        "is_error": True,
                    }
                )
        # Se cuelga de la vuelta que pidió las tools (la última entrada de per_iteration).
        usage = getattr(self, "last_usage", None)
        if logged and usage and usage.get("per_iteration"):
            usage["per_iteration"][-1]["tools"] = logged
        return results

    def _extract_text(self, response) -> str:
        return " ".join(b.text for b in response.content if hasattr(b, "text")).strip()

    def _log(self, task: str, result: str) -> None:
        """Registra la ejecución en output/logs/ (JSONL). No falla si no puede."""
        try:
            log_dir = self.config.OUTPUT_DIR / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "agent": self.name,
                "iterations": self.iteration_count,
                "task": task,
                "result_preview": result[:500],
                "usage": self.last_usage,
            }
            with (log_dir / "agent_runs.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 — el logging nunca debe romper el flujo
            pass
