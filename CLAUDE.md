# CLAUDE.md — dev-agent

Guía para trabajar en este proyecto. Léela antes de tocar código.

---

## Iniciar el agente (instrucciones para Claude Code)

Cuando el developer diga "inicia el agente", "abre el agente" o similar
→ ejecuta en tu terminal: `minka`

Backend y perfil AWS se configuran en `<DATA_DIR>/config_local.py` o en
`<DATA_DIR>/.env` (LLM_BACKEND, AWS_PROFILE).

**Proyectos (Arnés Global):**
- `minka` → usa `DEFAULT_PROJECT` de `<DATA_DIR>/config_local.py`
- `minka --project <nombre>` → cambia de proyecto
- Proyecto nuevo: `minka --new-project <nombre>` (ver `projects/_template/README.md`
  y `docs/ONBOARDING.md`)
- Onboarding y diagnóstico: `minka init` (guiado, nunca sobrescribe) y `minka doctor`
  (✓/✗: Python, uv, sesión, REPOS, chromadb, proyecto, contexto vivo)

**Comandos y flags de la CLI:**
- `minka "pedido"` / `minka` (REPL). `--agent <a>` salta el planner; sale completo salvo que el
  pedido sea una consulta de información simple (misma regla que `info_direct`): entonces breve.
- `--completo` fuerza la respuesta completa; por defecto las consultas puntuales
  (`router=info_direct`) salen breves con todas las citas y una nota "respuesta acotada".
  "en detalle" en el pedido hace lo mismo. `--debug` también desactiva el perfil breve.
- `--plain` / `MINKA_PLAIN=1`: salida sin renderizar Markdown (`core/render.py`). Sin TTY,
  con `--quiet`, `--json-out` o `MINKA_SOURCE=bridge` ya sale cruda.
- `minka stats [--day|--month] [--project] [--agent] [--source]`: tokens, cache, costo,
  rutas del router, cierres recortados, citas no encontradas y `prompt_hash` (línea base:
  sha256 de prompts + tools + MODELS; si aparece más de uno en el rango, las
  comparaciones no son válidas). Si el gasto del día alcanza `DAILY_MAX_USD`, `stats` y
  `doctor` avisan (informativo, nunca bloquea; excluye `eval_quality`).
- **Freno de gasto** (`core/orchestrator/budget.py`, lote 5): antes de lanzar un agente el orquestador
  levanta `BudgetGate` si la sesión alcanzó `SESSION_MAX_USD` (5.0; suma planner e interrumpidas) o
  si hay ráfaga (`BURST_N`=3 corridas con agente seguidas a menos de `BURST_GAP_SECONDS`=5 s del fin
  de la anterior). Las capas de $0 nunca frenan. REPL con TTY: pregunta; "sí" sube el tope otro
  escalón o reinicia la ráfaga, "no" descarta el pedido. Sin TTY o `MINKA_SOURCE=bridge`: una línea
  y exit 3. No aplica a `evals quality` ni al bridge. `/status` muestra el $ de la sesión.
- **Estimado previo**: con router `llm` o plan de 2+ pasos la línea de decisión añade
  `estimado ~$X (mediana de N corridas)` (mediana real de esa ruta en el mes, mínimo
  `ESTIMATE_MIN_RUNS`=5; si no, `sin estimado`, nunca un rango inventado). Pide confirmación
  (`BudgetGate` kind `estimado`) si supera `ESTIMATE_CONFIRM_USD` (0.50) o rompería el tope; tras un
  "sí" se relanza sin segunda llamada al planner. `info_direct`, `inherit` y las capas de $0 no lo muestran.
- `minka knowledge check --project P`: verifica las citas `archivo:línea` de
  `knowledge/extracted/` y del catálogo de riesgos contra REPOS (exit 1 si hay rotas).
- `minka evals routing --runs N`: mide el planner (cuesta tokens) con los casos `eval=info` del
  paquete más, si existe, `projects/<p>/evals/routing_cases.json`. `--from-feedback` (sin LLM)
  convierte `evals/routing_feedback.jsonl` (lo que marcaste con `/mal`) en
  `evals/routing_cases.draft.json` para revisar a mano. Ambos archivos contienen texto de tus
  pedidos: no versionarlos.
- `minka --version`: versión, commit y `requested_revision`; `minka trace [run_id|--last|--clear]`
  lee las trazas locales (solo existen con `TRACE_ENABLED` o `--trace`; ver `docs/OPERACION.md`).
- Fallos de red/timeout/throttling/credenciales durante una corrida devuelven un **resultado
  parcial** con la línea `⚠ corrida interrumpida: <tipo>, N vueltas completadas`; no es turno
  útil ni acierto de evals, la CLI sale con 1 y el REPL sigue. `TypeError` y Ctrl-C suben.

**Router (`core/orchestrator/router.py`), capas en orden:** saludo/estado/meta/ambiguo
(costo 0) → `inherit_repo` ("¿y en el repo hsm?" tras una consulta útil de architecture con citas:
repite el análisis sobre ese alias, sin planner) → `info_direct` si `PLANNER_SKIP_INFO=true` y el pedido es una pregunta de
información simple (architecture directo, sin planner) → planner LLM con reglas duras
(`no_plan`, `planner_error`, `followup_change`, `inherit`, `no_changes`). "¿En qué
quedamos?" al abrir la sesión responde con el contexto vivo sin modelo.

**Contexto vivo y citas:**
- `ACTIVE_CONTEXT_DIR` (default `<DATA_DIR>/active-context`, override en
  `config_local.py`): `<proyecto>.md` + última entrada real del `session_log` viajan como
  primer bloque del mensaje user (nunca en el system). Topes `LIVE_CONTEXT_MAX_LINES` /
  `LIVE_CONTEXT_MAX_TOKENS`; aviso si supera `LIVE_CONTEXT_STALE_DAYS`.
- `CITATION_CHECK_ENABLED` (default true): las citas de la respuesta se verifican contra
  REPOS y se anotan `⚠ no existe`; el texto anotado es el que hereda la memoria.

**Si hay error de sesión AWS:**
```bash
aws sso login --profile bedrock
```
Luego: `minka`

**Comandos disponibles dentro del chat:**
- `/status`  → estado del sistema (sesión AWS, backend, repos, memoria, $ de la sesión, nivel)
- `/clear`   → limpia el historial de la sesión
- `/mal [agente] [nota]` → marca la última decisión del router como equivocada (ver Freno de gasto arriba)
- `salir`    → termina la sesión
- Enter envía; Alt+Enter inserta un salto de línea (en Terminal/iTerm2 activar "Option as
  Meta"). Un pegado de `PASTE_COLLAPSE_LINES` (3) líneas o `PASTE_COLLAPSE_CHARS` (500)
  caracteres se colapsa en `[Pegado #N +M líneas]` y se expande al enviarlo; desde
  `PASTE_WARN_TOKENS` (2000) estimados avisa el tamaño. Solo con TTY (`core/repl_input.py`,
  prompt_toolkit, historial en memoria); bridge, tuberías y scripts siguen con `input()`.
- `minka --debug` → muestra input/resultado de cada tool call

**Configuración:**
- venv: `.venv/`
- Perfil AWS: `bedrock`, región: `us-east-1`

---

## Qué hace el proyecto

Sistema **multi-agente de IA (Python)** que analiza, mejora y escribe código en
los microservicios Go de cada proyecto configurado. Cada proyecto declara sus repos en
`config_local.py` → `REPOS` con alias genéricos, por ejemplo `pos`, `hsm`, `core` y un
módulo compartido como `payment-crypto`; los alias se usan en las citas (`POS/...`).

Escalable por **niveles de autonomía (3 → 6)** mediante flags de configuración;
subir de nivel es cambiar **configuración**, no código.

---

## Arquitectura

Arnés Global: motor genérico en `core/`, contexto por proyecto en `projects/`.

```
main.py → core/orchestrator (planner + state) → agente(s) especializado(s)
                                                   ↓ usan
                            core/tools/       (filesystem, git, análisis, RAG)
                            core/knowledge/   (loader + .md genéricos)
                            core/memory/      (sesión + persistente)
                            projects/<X>/     (config, prompts, knowledge)
escritura en repos → core/orchestrator/approval_gate.py (diff + confirmación)
```

**Archivos clave con la lógica de negocio:**
- `core/agents/base_agent.py` — núcleo ReAct (Reason+Act) de todos los agentes:
  loop de tool-calling, memoria de sesión, knowledge inyectada, selección de
  cliente LLM (dual, `build_llm_client`) y model ID (`resolve_model_id`).
- `core/orchestrator/orchestrator.py` — Plan-and-Execute: planifica, ejecuta agentes
  en secuencia pasando contexto, consolida resultados. Gate de sesión AWS al inicio.
- `core/orchestrator/planner.py` — decide qué agente(s) y en qué orden.
- `core/prompt_loader.py` — carga el system prompt desde el proyecto activo
  (fallback a `core/prompts/`).
- `config.py` + `config_loader.py` — merge en 3 capas: global → proyecto → local.
- `aws_session_manager.py` — sesión AWS SSO para Bedrock (3 capas de renovación).

**Directorios:**
- `core/agents/` — architecture, development, pr_review, debug, report, meta (Nivel 6).
- `core/orchestrator/` — planificación, estado, gate de aprobación.
- `core/tools/` — funciones que usan los agentes (lectura, escritura, git, análisis, RAG).
- `core/knowledge/` — loader + `professional/` genérico (go, hexagonal, patterns, checklist).
- `core/memory/` — memoria de sesión y persistente (Niveles 4-5 en stub).
- `core/evaluator/` — evaluación y auto-mejora (Niveles 5-6 en stub).
- `<DATA_DIR>/projects/<proyecto>/` — config, prompts, knowledge (extracted/professional
  /team) de cada proyecto; viven fuera del repo del motor. El contexto de corto plazo
  vive en `ACTIVE_CONTEXT_DIR/<proyecto>.md`, fuente única de verdad.
- `projects/_template/` — plantilla para proyectos nuevos (ver su README.md).

---

## Backend dual: Bedrock (SSO) / API directa

Los agentes pueden hablar con Claude por dos vías, seleccionables sin tocar código:

| Backend | Cliente | Auth |
|---------|---------|------|
| **Amazon Bedrock** | `anthropic.AnthropicBedrock(aws_profile, aws_region)` | Perfil AWS SSO |
| **API directa** (default) | `anthropic.Anthropic()` | `ANTHROPIC_API_KEY` |

**Selección** (en `config.py`):
- `LLM_BACKEND=bedrock` → Bedrock. `LLM_BACKEND=anthropic` (o vacío) → API directa.
- Si `LLM_BACKEND` está vacío pero existe `AWS_PROFILE` en el entorno → asume Bedrock.

```bash
export AWS_PROFILE=bedrock      # Bedrock con el perfil correcto
export LLM_BACKEND=anthropic    # fuerza API directa
```

> ⚠️ Si tu shell ya exporta `AWS_PROFILE` (p.ej. `default`), el agente asumirá
> Bedrock automáticamente con ese perfil. Usa las variables de arriba para
> controlarlo explícitamente.

**Model IDs — prefijo de inference profile regional.**
`config.MODELS` usa IDs "bare" (`claude-opus-4-8`). En modo Bedrock,
`resolve_model_id()` antepone `config.BEDROCK_MODEL_PREFIX` (default
`us.anthropic.`) de forma **idempotente** (normaliza cualquier variante; nunca
duplica ni apila prefijos). Los modelos on-demand de Bedrock **exigen** este
prefijo regional (`us.` / `eu.` / `apac.`) — un ID sin él da **400
"on-demand throughput isn't supported"**. Cámbialo con `BEDROCK_MODEL_PREFIX`.

**Renovación de sesión (3 capas):**
- **Capa 1+2** (auto-refresh de credenciales + relectura del token SSO del cache)
  las hace **botocore** solo al crear la `boto3.Session`. No se reimplementan.
- **Capa 3** es propia: `AWSSessionManager.ensure_sso_login()` → `(ok, mensaje)`.
  Token vigente: sin red. Vencido: valida con STS (timeouts cortos). Solo ante un
  error de token lanza `aws sso login --profile <AWS_PROFILE>` (con TTY y
  `SSO_LOGIN_TIMEOUT_SECONDS`); sin TTY o sin red devuelve el mensaje, sin esperar.
  El orchestrator la invoca antes de cada tarea (solo en modo Bedrock).

---

## Configuración inicial AWS SSO (una sola vez)

```bash
minka-sso            # crea/verifica el perfil "bedrock" (abre browser)
minka-sso --verify   # checklist completo + ping real a Bedrock
```

Si ves "Sesión expirada" durante una tarea, en otra terminal:
```bash
aws sso login --profile bedrock
```

`sso_start_url`, `sso_account_id` y `sso_role_name` viven en `~/.aws/config`
(del usuario, local) — **nunca** en el repo. El token vive en `~/.aws/sso/cache/`.

---

## Patrón de configuración (`config_loader`)

**Regla:** todo módulo accede a la config vía `from config_loader import config`.

- `config.py` — defaults del proyecto (constantes en MAYÚSCULAS).
- `config_local.py` — overrides locales de cada dev (rutas de repos, nivel).
  Está en `.gitignore`, **no se commitea**. Se crea copiando `config.example.py`.
- `config_loader.py` — funde ambos: toma las MAYÚSCULAS de `config.py`, las
  sobreescribe con las de `config_local.py`, y expone un `SimpleNamespace`.

> ⚠️ `config_loader` **solo copia constantes en MAYÚSCULAS**. Si agregas una
> **función** al módulo base que los agentes necesiten vía `config` (p.ej.
> `get_aws_session`), debes adjuntarla explícitamente en `config_loader.load_config()`
> — no se propaga sola.

---

## Niveles de autonomía

| Nivel | Flags | Comportamiento |
|-------|-------|----------------|
| **3** (hoy) | `AUTONOMY_LEVEL=3`, `AUTO_COMMIT=False` | Toda escritura pasa por aprobación manual (gate con diff). |
| **4** | `RAG_ENABLED=True` | Búsqueda semántica del código (ChromaDB). |
| **5** | `EVALUATOR_ENABLED=True` | Fix-loop development → revisión → fix (tope `FIX_LOOP_MAX_USD`) y `--evaluate`. |
| **6** | `META_AGENT_ENABLED=True`, `PROMPT_OPTIMIZATION_ENABLED=True` | El sistema mejora sus propios prompts. |

---

## Convenciones de código

- **Nunca lanzar excepciones de infraestructura (AWS, red) hacia el agente.**
  Las funciones de sesión retornan `(bool, mensaje)` (`ensure_sso_login`) o `None`
  (`sso_token_status`), nunca propagan `try/catch` de boto3 hacia arriba. El
  agente/orchestrator reacciona a valores, no atrapa excepciones de AWS.
- **Retornar `bool` para "¿puedo continuar?".** El orchestrator solo pregunta
  "¿está la sesión válida?" y actúa según la respuesta; no sabe nada de AWS.
- **Diffs antes de modificar archivos existentes.** Mostrar el diff y esperar
  confirmación antes de editar cualquier archivo ya versionado. Los archivos
  nuevos se pueden crear directamente.
- **Lazy init de recursos costosos.** Clientes LLM y `AWSSessionManager` se
  crean perezosamente (`get_aws_session()`), para que `minka --help`
  no falle si AWS/API key aún no están configurados.
- **Helpers idempotentes.** Normalizadores como `resolve_model_id()` deben ser
  seguros ante entradas ya normalizadas (no duplicar prefijos).
- **`time.monotonic()` para esperas/polling**, no `time.time()` (inmune a
  cambios del reloj del sistema).
- **Nunca hardcodear secretos** ni loggear datos de tarjeta (PCI-DSS). No poner
  `sso_start_url`, tokens ni API keys en archivos versionados.
- **El logging nunca rompe el flujo** (`_log()` en `base_agent.py` traga errores).
- **Los commits los hace el humano** (hasta Nivel 5).

---

## Entorno de desarrollo

- Python ≥3.12 (uv recomendado). Venv del proyecto: `.venv/` (en `.gitignore`).
- Instalar para desarrollar: `uv sync --extra dev` (dependencias en `pyproject.toml`;
  no hay requirements.txt). Tests: `uv run pytest`; lint: `uv run ruff check`.
- Dependencias AWS: `anthropic[bedrock]`, `boto3`, `botocore`. AWS CLI para SSO.
- La suite es hermética (`tests/conftest.py`): borra `MINKA_*`, `AWS_*` y `ANTHROPIC_API_KEY`,
  usa `HOME`/`MINKA_HOME` temporales. Debe pasar igual con `MINKA_QUIET=1 MINKA_SOURCE=bridge
  MINKA_PLAIN=1` exportadas. Los tests nunca llaman al modelo: clientes falsos.
- CI (`.github/workflows/ci.yml`): pytest 3.12/3.13 con lock y sin lock, ruff, escaneo de secretos,
  `prompt_hash` del motor contra `baseline/prompt_hash_motor.txt` (cómo moverlo: `baseline/README.md`),
  build + instalación aislada, `release.sh --dry-run`, versión == tag en push de tag.
- Publicar: `scripts/release.sh vX.Y.Z` (tag local + verificación; el push lo hace el dev a mano).
  Revertir: `scripts/rollback.sh vANTERIOR`. Ambos se prueban con `scripts/verify_release_scripts.sh`
  contra un repo temporal. Operación diaria y retención: `docs/OPERACION.md`.