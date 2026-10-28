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
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import anthropic
from rich.console import Console
from rich.panel import Panel

from minka.config_loader import config
from minka.core.knowledge.loader import KnowledgeLoader
from minka.core.memory.session_memory import SessionMemory
from minka.core.memory.vector_memory import VectorMemory
from minka.core.observability import trace
from minka.core.tools.registry import ToolRegistry

console = Console()


def build_llm_client():
    """Crea el cliente Anthropic según el backend configurado.

    - Bedrock: usa el perfil SSO (aws_profile/aws_region). La renovación
      automática de credenciales la maneja botocore (Capas 1-2).
    - API directa: anthropic.Anthropic() con ANTHROPIC_API_KEY.

    Módulo-nivel para que otros componentes (planner) usen el mismo backend
    que los agentes sin duplicar la lógica.
    """
    if getattr(config, "USE_BEDROCK", False):
        # Inicializa el session manager (lazy) para dejar la sesión lista.
        config.get_aws_session()
        return anthropic.AnthropicBedrock(
            aws_profile=config.AWS_PROFILE,
            aws_region=config.AWS_REGION,
        )
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


class BaseAgent(ABC):
    def __init__(self, name: str):
        self.name = name
        self.config = config
        self.client = self._build_client()
        self.memory = SessionMemory(agent_name=name)
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
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            ):
                self.last_usage[key] += getattr(usage, key, 0) or 0
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
        console.print(Panel(f"[bold]{self.name}[/bold] recibió tarea", style="blue"))

        with trace(f"agent:{self.name}", agent=self.name):
            recalled = self._recall_memories(task)
            user_content = f"{recalled}\n\n{task}" if recalled else task
            messages = self.memory.get_context() + [{"role": "user", "content": user_content}]
            self.iteration_count = 0
            self.last_usage = self._empty_usage()

            # Se construye UNA vez, fuera del loop: el prefijo cacheado debe ser
            # byte-idéntico en cada iteración o el cache no acierta.
            system = self._system_param()

            while self.iteration_count < self.max_iterations:
                self.iteration_count += 1

                response = self.client.messages.create(
                    model=self._model(),
                    max_tokens=self.config.MAX_TOKENS,
                    system=system,
                    tools=self.tools,
                    messages=messages,
                )
                self._accumulate_usage(response)

                if response.stop_reason == "tool_use":
                    tool_results = self._execute_tools(response)
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append({"role": "user", "content": tool_results})
                    continue

                # end_turn u otro → respuesta final
                result = self._extract_text(response)
                self.memory.add_exchange(task, result)
                self._remember(task, result)
                self._log(task, result)
                return result

            return "Máximo de iteraciones alcanzado sin respuesta final."

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
        for block in response.content:
            if block.type != "tool_use":
                continue
            console.print(f"  [dim]→ Tool:[/dim] [cyan]{block.name}[/cyan]")
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
                output = tool_fn(**kwargs)
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
