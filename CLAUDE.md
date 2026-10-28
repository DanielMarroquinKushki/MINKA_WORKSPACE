# CLAUDE.md — dev-agent

Guía para trabajar en este proyecto. Léela antes de tocar código.

---

## Iniciar el agente (instrucciones para Claude Code)

Cuando el developer diga "inicia el agente", "abre el agente" o similar
→ ejecuta en tu terminal: `./agent`

**Regla de oro: NUNCA pedirle al developer que active el venv ni exporte
variables.** `./agent` lo hace todo automáticamente (venv, AWS_PROFILE,
LLM_BACKEND). Para iniciar sin configuración: solo `./agent`.

**Proyectos (Arnés Global):**
- `./agent` → usa `DEFAULT_PROJECT` de `config_local.py` (proyecto de referencia: amex-colombia)
- `./agent --project <nombre>` → cambia de proyecto
- Proyecto nuevo: ver `projects/_template/README.md`

**Si hay error de sesión AWS:**
```bash
aws sso login --profile bedrock
```
Luego: `./agent`

**Comandos disponibles dentro del chat:**
- `/status`  → estado del sistema (sesión AWS, backend, repos, memoria, nivel)
- `/clear`   → limpia el historial de la sesión
- `salir`    → termina la sesión
- `./agent --debug` → muestra input/resultado de cada tool call

**Configuración:**
- venv: `.venv/`
- Perfil AWS: `bedrock`, región: `us-east-1`

**Contexto del proyecto:** lee `CONTEXTO_MAESTRO_DEV_AGENT.md` (raíz del
proyecto) para entender la arquitectura completa antes de modificar
cualquier archivo.

---

## Qué hace el proyecto

Sistema **multi-agente de IA (Python)** que analiza, mejora y escribe código en
los microservicios Go de cada proyecto configurado. Proyecto de referencia
(amex-colombia): `usrv-acq-pos`, `usrv-acq-hsm`, `usrv-go-core`.

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
- `projects/amex-colombia/` — config, prompts, knowledge (extracted/professional
  pagos/team). El contexto de corto plazo vive en `active-context/[proyecto].md`
  (raíz del workspace), fuente única de verdad.
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
- **Capa 3** (pausa elegante) es propia: `AWSSessionManager.ensure_valid_session()`
  muestra instrucciones, espera hasta 10 min (polling 30s) y retoma si renuevas.
  El orchestrator la invoca antes de cada tarea larga (solo en modo Bedrock).

---

## Configuración inicial AWS SSO (una sola vez)

```bash
python setup_sso.py            # crea/verifica el perfil "bedrock" (abre browser)
python setup_sso.py --verify   # checklist completo + ping real a Bedrock
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
| **5** | `EVALUATOR_ENABLED=True`, `RUN_TESTS_AFTER_CHANGE=True` | Auto-aprueba si los tests Go pasan. |
| **6** | `META_AGENT_ENABLED=True`, `PROMPT_OPTIMIZATION_ENABLED=True` | El sistema mejora sus propios prompts. |

---

## Convenciones de código

- **Nunca lanzar excepciones de infraestructura (AWS, red) hacia el agente.**
  Las funciones de sesión retornan `bool` (`ensure_valid_session`) o `None`
  (`sso_token_status`), nunca propagan `try/catch` de boto3 hacia arriba. El
  agente/orchestrator reacciona a valores, no atrapa excepciones de AWS.
- **Retornar `bool` para "¿puedo continuar?".** El orchestrator solo pregunta
  "¿está la sesión válida?" y actúa según la respuesta; no sabe nada de AWS.
- **Diffs antes de modificar archivos existentes.** Mostrar el diff y esperar
  confirmación antes de editar cualquier archivo ya versionado. Los archivos
  nuevos se pueden crear directamente.
- **Lazy init de recursos costosos.** Clientes LLM y `AWSSessionManager` se
  crean perezosamente (`get_aws_session()`), para que `python main.py --help`
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
- Instalar: `python -m venv .venv && ./.venv/bin/pip install -r requirements.txt`.
- Dependencias AWS: `anthropic[bedrock]`, `boto3`, `botocore`. AWS CLI para SSO.