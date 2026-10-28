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

from cincha._app import APP_NAME, DATA_DIR_ENV

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
# Repos Go — VACÍOS por defecto. Se completan en config_local.py.
# NOTA: la ruta real en este equipo es:
#   credibanco_project/microservices_amex_colombia/{usrv-acq-pos,usrv-acq-hsm,usrv-go-core}
# (el prompt original decía credibanco/ pero esa carpeta no existe)
# ---------------------------------------------------------------------------
REPOS = {
    "pos": "",
    "hsm": "",
    "core": "",
}

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
# Si LLM_BACKEND no está seteado pero SÍ existe AWS_PROFILE, se asume bedrock.
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
    """Devuelve el AWSSessionManager compartido (lo crea la primera vez).
    Solo se llama cuando USE_BEDROCK es True."""
    global _aws_session
    if _aws_session is None:
        from cincha.aws_session_manager import AWSSessionManager

        _aws_session = AWSSessionManager(
            profile_name=AWS_PROFILE,
            region=AWS_REGION,
            refresh_before_minutes=BEDROCK_REFRESH_BEFORE_MINUTES,
        )
    return _aws_session
