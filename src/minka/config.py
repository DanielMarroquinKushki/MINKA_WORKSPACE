"""
config.py — Configuración base del sistema dev-agent.

Estos valores son los defaults del proyecto. Cada dev crea su propio
`<DATA_DIR>/config_local.py` (copiando `config.example.py`) con SUS rutas de repos;
vive fuera del repo, con los datos del usuario (ver DATA_DIR más abajo).

Carga: los módulos deben usar `from config_loader import config` para obtener
la configuración efectiva (base + overrides locales). Ver config_loader.py.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from minka._app import APP_NAME, DATA_DIR_ENV

# ---------------------------------------------------------------------------
# Rutas base
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "core" / "knowledge"
# Plantilla de proyecto nuevo: es código del motor, no datos del usuario.
TEMPLATE_DIR = BASE_DIR / "projects" / "_template"

# Datos del usuario (proyectos, salida, índice RAG), fuera del paquete.
# El nombre (APP_NAME, DATA_DIR_ENV) vive solo en _app.py.
# DATA_DIR solo se cambia con la variable de entorno, no con config_local.py:
# las rutas derivadas se calculan aquí, antes de que config_local se aplique.
DATA_DIR = Path(os.getenv(DATA_DIR_ENV) or Path.home() / f".{APP_NAME}").expanduser()
# .env del usuario, ANTES de cualquier os.getenv de abajo. No pisa el entorno del shell.
load_dotenv(DATA_DIR / ".env", override=False)
PROJECTS_DIR = DATA_DIR / "projects"
OUTPUT_DIR = DATA_DIR / "output"
# deepeval lee esta variable al importarse; el motor lo importa después
# (core/evaluator/regression_evals.py). En pytest su plugin va desactivado.
os.environ.setdefault("DEEPEVAL_CACHE_FOLDER", str(DATA_DIR / ".deepeval"))

# ---------------------------------------------------------------------------
# Model routing — modelo óptimo por tipo de tarea.
# IDs "bare" (sin prefijo de proveedor): resolve_model_id() antepone
# BEDROCK_MODEL_PREFIX en modo Bedrock y los deja tal cual en API directa.
# Override por env (MODEL_*) o en config_local.py.
# ---------------------------------------------------------------------------
MAX_TOKENS = 8192
CONTEXT_WINDOW = 1_000_000  # 1M tokens — permite leer repos completos

MODELS = {
    # Razonamiento complejo — análisis profundo, código crítico
    "orchestrator": os.getenv("MODEL_ORCHESTRATOR", "claude-opus-4-8"),
    "architecture_agent": os.getenv("MODEL_ARCHITECTURE", "claude-opus-4-8"),
    "development_agent": os.getenv("MODEL_DEVELOPMENT", "claude-opus-4-8"),
    "debug_agent": os.getenv("MODEL_DEBUG", "claude-opus-4-8"),
    "meta_agent": os.getenv("MODEL_META", "claude-opus-4-8"),
    # Tareas rápidas — revisión, planeación, reportes
    "pr_review_agent": os.getenv("MODEL_PR_REVIEW", "claude-sonnet-5"),
    "planner": os.getenv("MODEL_PLANNER", "claude-sonnet-5"),
    # OJO con el sufijo -v1:0: en Bedrock los modelos nuevos son "bare"
    # (claude-opus-4-8, claude-sonnet-5) pero los snapshots con fecha exigen
    # el sufijo de versión. Sin él: 400 "The provided model identifier is
    # invalid". resolve_model_id() solo antepone el prefijo regional, no el
    # sufijo. Verificado con `aws bedrock list-inference-profiles` (2026-09-22).
    "report_agent": os.getenv("MODEL_REPORT", "claude-haiku-4-5-20251001-v1:0"),
}

# Fallback — si un agente no está en MODELS usa este.
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "claude-sonnet-5")

# Legacy — compatibilidad con código que aún use ANTHROPIC_MODEL.
ANTHROPIC_MODEL = MODELS.get("development_agent", DEFAULT_MODEL)

# ---------------------------------------------------------------------------
# Repos del proyecto: {alias: ruta}. Viven en projects/<p>/config.py con rutas relativas
# a REPOS_ROOT; config_local.py aporta REPOS_ROOT (la carpeta de TU equipo) y, si hace
# falta, REPOS_OVERRIDES {alias: ruta} para pisar rutas puntuales. config_loader.resolve_repos
# funde las capas y deja el resultado en config.REPOS (lo que leen tools, citas, indexer,
# doctor…). El esquema viejo (REPOS en config_local) sigue funcionando; `minka doctor`
# avisa la deprecación. REPOS_SOURCE lo fija el loader: "project" | "local" | "none".
# ---------------------------------------------------------------------------
REPOS: dict = {}
REPOS_ROOT: str | None = None
REPOS_OVERRIDES: dict = {}
REPOS_SOURCE = "none"

# Stack del proyecto (core/stacks.py): "go" | "python" | "node". None = "go" y esquema
# viejo. Decide extensiones de código, comando de tests, archivo de dependencias, knowledge
# profesional por defecto y qué tools exclusivas de Go se registran.
STACK: str | None = None
# Cumplimiento opcional, p. ej. ["pci"]: habilita check_pci_compliance y la checklist PCI.
# None = esquema viejo: ["pci"] solo si STACK también es None; con STACK declarado, [].
COMPLIANCE: list | None = None

# ---------------------------------------------------------------------------
# Parametrización por proyecto (Arnés Global)
# Los valores reales viven en projects/<proyecto>/config.py. Aquí solo hay
# defaults vacíos seguros para correr sin proyecto activo (p.ej. --help).
# ---------------------------------------------------------------------------
# Nombre del módulo compartido cuya versión se compara entre repos.
CORE_MODULE_NAME = ""

# Pilares/valores del equipo que el gate y las lentes recuerdan. Vacío = se omiten.
TEAM_PILLARS: list = []
TEAM_PILLARS_HEADING = "Pilares del equipo"
TEAM_PILLARS_READABILITY = ""  # referencia en la lente de Legibilidad (4R)
TEAM_PILLARS_LINE_CAP_HINT = ""  # línea extra en el aviso de Line Cap

# Reglas de riesgo extra del proyecto: (id, severidad, regex, descripción).
# Un id que ya existe en el core lo reemplaza en su posición; si no, se agrega.
RISK_PATTERNS: list = []
# Alertas extra de validate_diff: (needle, mensaje). Mismo criterio por needle.
PR_DIFF_ALERTS: list = []

# Checklist que se incrusta en cada descripción de PR generada.
PR_CHECKLIST: list = []

# Qué archivos .md de knowledge/ ve cada agente.
AGENT_KNOWLEDGE: dict = {}

# Qué tools puede usar cada agente.
AGENT_TOOLS: dict = {}

# ---------------------------------------------------------------------------
# Autonomía
# ---------------------------------------------------------------------------
AUTONOMY_LEVEL = 3  # 3=requiere aprobación, 5=semi-autónomo, 6=autónomo
DEBUG = False  # --debug en main.py lo activa: muestra input/output de cada tool
QUIET = False  # --quiet en main.py: sin líneas de decisión, métricas ni avisos en stderr
PROGRESS_ENABLED = True  # main.py lo apaga con --json-out; core/progress.py también exige TTY
# Historial único de la sesión (core/memory/conversation_memory.py): turnos útiles que
# viajan a cada agente como pares user/assistant y tope de chars del resultado guardado.
HISTORY_MAX_TURNS = int(os.getenv("HISTORY_MAX_TURNS", "5"))
HISTORY_RESULT_CHARS = int(os.getenv("HISTORY_RESULT_CHARS", "1500"))
# Pegados en el REPL (core/repl_input.py, solo con TTY): un pegado de al menos
# PASTE_COLLAPSE_LINES líneas o PASTE_COLLAPSE_CHARS caracteres se muestra como
# "[Pegado #N +M líneas]" y se expande al enviarlo; desde PASTE_WARN_TOKENS estimados avisa.
PASTE_COLLAPSE_LINES = int(os.getenv("PASTE_COLLAPSE_LINES", "3"))
PASTE_COLLAPSE_CHARS = int(os.getenv("PASTE_COLLAPSE_CHARS", "500"))
PASTE_WARN_TOKENS = int(os.getenv("PASTE_WARN_TOKENS", "2000"))
# Contexto vivo del proyecto (core/memory/live_context.py): <proyecto>.md de
# ACTIVE_CONTEXT_DIR + última entrada real del session_log, como primer bloque del mensaje
# user (nunca en el system). None = <DATA_DIR>/active-context; se sobreescribe en
# config_local.py (o con la env ACTIVE_CONTEXT_DIR) cuando vive fuera del directorio de datos.
ACTIVE_CONTEXT_DIR = os.getenv("ACTIVE_CONTEXT_DIR") or None
LIVE_CONTEXT_ENABLED = os.getenv("LIVE_CONTEXT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
LIVE_CONTEXT_MAX_LINES = int(os.getenv("LIVE_CONTEXT_MAX_LINES", "60"))
LIVE_CONTEXT_MAX_TOKENS = int(os.getenv("LIVE_CONTEXT_MAX_TOKENS", "750"))
LIVE_CONTEXT_STALE_DAYS = int(os.getenv("LIVE_CONTEXT_STALE_DAYS", "7"))
# Verificación de citas archivo:línea de la respuesta contra REPOS
# (core/orchestrator/citation_check.py): anota "⚠ no existe" y cuenta en métricas.
CITATION_CHECK_ENABLED = os.getenv("CITATION_CHECK_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)
# Pregunta de información simple (sin verbo de escritura, fallo ni encadenado) →
# architecture directo, sin planner: $0 de routing y 100% determinista.
PLANNER_SKIP_INFO = os.getenv("PLANNER_SKIP_INFO", "true").strip().lower() in ("1", "true", "yes")
# Veces que una misma tool con los mismos argumentos puede ejecutarse por corrida.
TOOL_REPEAT_LIMIT = int(os.getenv("TOOL_REPEAT_LIMIT", "3"))
# Tope de vueltas del architecture_agent por corrida (core/agents/base_agent.py): en la vuelta
# tope-1 se le pide la respuesta final con lo recogido; la corrida cuenta en iterations_capped.
ARCH_MAX_ITERATIONS = int(os.getenv("ARCH_MAX_ITERATIONS", "8"))
# Plan de varios pasos: cuántos caracteres del resultado de cada paso previo ve el siguiente
# agente (state_manager.context_for_next). Si se recorta, se avisa en la línea de decisión.
CONTEXT_NEXT_MAX_CHARS = int(os.getenv("CONTEXT_NEXT_MAX_CHARS", "12000"))
# Verificador determinista de afirmaciones (core/orchestrator/claim_check.py): identificadores
# en backticks, bloques de código y cifras contra REPOS y el resultado previo; anota "⚠ sin respaldo".
VERIFY_CLAIMS_ENABLED = os.getenv("VERIFY_CLAIMS_ENABLED", "true").strip().lower() in ("1", "true", "yes")
AUTO_COMMIT = False  # True solo en Nivel 5+
AUTO_BRANCH = True  # Siempre crea rama antes de escribir
BRANCH_PREFIX = "agent/"

# Prompt caching: cachea el prefijo (tools + system) entre las iteraciones del
# loop ReAct. El contexto es idéntico en cada vuelta, así que sin cache se paga
# N veces. On por defecto: no cambia el comportamiento del agente, solo el costo.
# Requiere que el prefijo supere el mínimo cacheable del modelo (1024 tokens;
# 2048 en Haiku); por debajo de eso la API simplemente no cachea, sin error.
PROMPT_CACHE_ENABLED = os.getenv("PROMPT_CACHE_ENABLED", "true").strip().lower() in (
    "1",
    "true",
    "yes",
)

# TTL del prompt cache. "5m" (default de la API) o "1h". El write de 1h cuesta
# 2.00x el input (vs 1.25x del de 5m) y las lecturas siguen a 0.10x en ambos:
# solo conviene si hay >=2 corridas al MISMO agente dentro de la hora. Por agente
# a propósito — el prefijo cacheado es distinto en cada uno (KB distinta), así que
# el patrón de reuso también lo es.
CACHE_TTL_DEFAULT = os.getenv("CACHE_TTL_DEFAULT", "5m")
CACHE_TTL_BY_AGENT = {
    # Único con huecos medidos de 5-60 min entre consultas del mismo ticket.
    "architecture_agent": os.getenv("CACHE_TTL_ARCHITECTURE", "1h"),
}

# Techo de líneas (patrón RDD): si un cambio supera este umbral, el ApprovalGate
# ADVIERTE (no bloquea) sugiriendo dividirlo. Cuenta líneas añadidas + borradas.
LINE_CAP = int(os.getenv("LINE_CAP", "150"))

# ---------------------------------------------------------------------------
# RAG — Nivel 4 (stub por ahora)
# ---------------------------------------------------------------------------
RAG_ENABLED = False
CHROMA_PATH = DATA_DIR / ".chromadb"
EMBEDDING_MODEL = "text-embedding-3-small"

# Reranking (two-stage retrieval): reordena los candidatos de ChromaDB con un
# cross-encoder local. Off por defecto; actívalo en config_local.py.
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "").strip().lower() in ("1", "true", "yes")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")  # multilingüe
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "50"))  # candidatos antes de rerank

# Memoria semántica de largo plazo (mem0): destila sesiones en "hechos" y los
# recupera por similitud. Off por defecto; actívalo en config_local.py.
VECTOR_MEMORY_ENABLED = os.getenv("VECTOR_MEMORY_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)
# mem0 usa un LLM para extraer hechos: debe aceptar `temperature` (los Claude más
# nuevos la deprecaron), por eso el default es un Claude 3 (temperature-compatible).
MEM0_LLM_MODEL = os.getenv("MEM0_LLM_MODEL", "us.anthropic.claude-3-haiku-20240307-v1:0")
MEM0_EMBEDDER_MODEL = os.getenv("MEM0_EMBEDDER_MODEL", "amazon.titan-embed-text-v2:0")

# DeepEval usa un LLM como juez (G-Eval); también debe aceptar `temperature` → Claude 3.
EVAL_JUDGE_MODEL = os.getenv("EVAL_JUDGE_MODEL", "us.anthropic.claude-3-haiku-20240307-v1:0")

# Observabilidad (Langfuse): trazas del loop de agentes (tokens/costo/latencia).
# Off por defecto. Requiere credenciales LANGFUSE_* en el entorno y un servicio
# Langfuse (self-host Docker o cloud). Actívalo en config_local.py.
OBSERVABILITY_ENABLED = os.getenv("OBSERVABILITY_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# Prompt versioning (Langfuse Prompts): trae los prompts de un registro versionado
# en vez de los archivos *_prompt.py. Off por defecto (usa archivos). Requiere el
# servicio Langfuse + credenciales LANGFUSE_*. Actívalo en config_local.py.
PROMPT_REGISTRY_ENABLED = os.getenv("PROMPT_REGISTRY_ENABLED", "").strip().lower() in (
    "1",
    "true",
    "yes",
)

# ---------------------------------------------------------------------------
# Evaluador — Nivel 5+ (stub por ahora)
# ---------------------------------------------------------------------------
EVALUATOR_ENABLED = False
# Tope de costo de una corrida con fix-loop (tarifa de lista, core/pricing.py). 0 = sin tope.
FIX_LOOP_MAX_USD = float(os.getenv("FIX_LOOP_MAX_USD", "2.0"))
RUN_TESTS_AFTER_CHANGE = False
GO_TEST_TIMEOUT = "30s"  # legacy (formato flag de Go); usar GO_TEST_TIMEOUT_SECONDS
GO_TEST_TIMEOUT_SECONDS = int(os.getenv("GO_TEST_TIMEOUT_SECONDS", "60"))

# ---------------------------------------------------------------------------
# Meta-agente — Nivel 6 (stub por ahora)
# ---------------------------------------------------------------------------
META_AGENT_ENABLED = False
PROMPT_OPTIMIZATION_ENABLED = False

# ---------------------------------------------------------------------------
# AWS Bedrock / SSO — backend LLM opcional (modo dual)
# ---------------------------------------------------------------------------
# LLM_BACKEND controla qué cliente usan los agentes:
#   - "bedrock"           → anthropic.AnthropicBedrock (vía perfil SSO)
#   - "anthropic" / vacío → anthropic.Anthropic() (API key directa, default)
# Si LLM_BACKEND no está seteado pero SÍ hay AWS_PROFILE, se infiere bedrock y
# build_llm_client avisa (config_loader decide tras el merge con config_local).
AWS_PROFILE = os.getenv("AWS_PROFILE", "bedrock")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")

# Constante en MAYÚSCULAS para que config_local.py pueda sobreescribirla
# (config_loader recalcula USE_BEDROCK después del merge).
LLM_BACKEND = os.getenv("LLM_BACKEND", "").strip().lower()
if LLM_BACKEND:
    USE_BEDROCK = LLM_BACKEND == "bedrock"
else:
    # Sin LLM_BACKEND explícito: usa Bedrock solo si el usuario definió AWS_PROFILE.
    USE_BEDROCK = bool(os.getenv("AWS_PROFILE"))

# Minutos antes de expirar con los que se considera un token "por vencer".
BEDROCK_REFRESH_BEFORE_MINUTES = 5
# Saludos que se responden sin LLM (pedido completo, normalizado: minúsculas, sin
# tildes ni puntuación). Ver core/orchestrator/router.py.
SMALL_TALK_MAX_WORDS = 4
SMALL_TALK_PATTERNS = [
    r"(hola|buenas|buenos dias|buenas tardes|buenas noches|hey|hi|hello)( minka)?",
    r"(muchas )?gracias( minka)?|thanks|thank you",
    r"ok|okay|vale|listo|perfecto|genial|dale|de acuerdo",
    r"adios|chao|bye|hasta luego",
]

# Origen para reinstalar con extras. Vacío = se lee del paquete instalado
# (direct_url.json, PEP 610); si no hay, queda el texto de ejemplo.
INSTALL_SOURCE = ""

# Tope para `aws sso login` (espera a que completes el login en el navegador).
SSO_LOGIN_TIMEOUT_SECONDS = 300
# Cliente Bedrock: lectura y conexión (httpx.Timeout) y reintentos del SDK.
# La conexión también se usa como timeout de la validación STS.
BEDROCK_TIMEOUT_SECONDS = 180
BEDROCK_CONNECT_TIMEOUT_SECONDS = 10
BEDROCK_MAX_RETRIES = 2

# Prefijo de proveedor para los model IDs en Bedrock. Los modelos on-demand se
# invocan vía inference profile regional: "us.anthropic.", "eu.anthropic." o
# "apac.anthropic." según tu región. Cámbialo aquí (o con BEDROCK_MODEL_PREFIX
# en el entorno) sin tocar código.
BEDROCK_MODEL_PREFIX = os.getenv("BEDROCK_MODEL_PREFIX", "us.anthropic.")

# El AWSSessionManager se crea PEREZOSAMENTE (lazy) vía get_aws_session():
# así `python main.py --help` no falla si AWS aún no está configurado, y el
# modo API-directa nunca importa boto3.
_aws_session = None


def get_aws_session():
    """AWSSessionManager compartido para el perfil y la región EFECTIVOS
    (config_local > entorno > "bedrock", resueltos por config_loader). Se recrea
    si cambian (p. ej. tras set_project). Solo se llama cuando USE_BEDROCK es True."""
    global _aws_session
    from minka.aws_session_manager import AWSSessionManager
    from minka.config_loader import config as cfg

    key = (cfg.AWS_PROFILE, cfg.AWS_REGION)
    if _aws_session is None or (_aws_session.profile_name, _aws_session.region) != key:
        _aws_session = AWSSessionManager(
            profile_name=cfg.AWS_PROFILE,
            region=cfg.AWS_REGION,
            refresh_before_minutes=cfg.BEDROCK_REFRESH_BEFORE_MINUTES,
            login_timeout_seconds=cfg.SSO_LOGIN_TIMEOUT_SECONDS,
            connect_timeout_seconds=cfg.BEDROCK_CONNECT_TIMEOUT_SECONDS,
        )
    return _aws_session
