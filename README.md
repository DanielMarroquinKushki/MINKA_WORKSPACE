# kushki-workspace — dev-agent

Sistema multi-agente de IA para desarrollo de software.
Analiza, revisa y modifica código respetando los estándares
técnicos y culturales del equipo.

**Para empezar:** lee `ONBOARDING.md` en la raíz del workspace.

Escalable por niveles de autonomía (3 → 6) mediante flags de configuración.

---

## Instalación

```bash
# 1. Clonar el agente
git clone <repo-dev-agent>
cd dev-agent

# 2. Instalar dependencias (idealmente en un venv)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Configurar tus rutas locales de repos
cp config.example.py config_local.py
#   edita config_local.py con TUS rutas (config_local.py está en .gitignore)

# 4. Autenticación AWS SSO (ver sección "Autenticación" más abajo)
python setup_sso.py

# 5. Verificar
python main.py --help

# 6. Usar
python main.py
```

> Nivel 4 (RAG): `pip install chromadb`, pon `RAG_ENABLED=True` y luego
> `python main.py --index-repos`.

---

## Uso diario

### Iniciar el agente

```bash
cd [ruta-workspace]/dev-agent
source .venv/bin/activate
python main.py
```

### Si la sesión AWS expira (máximo una vez por jornada)

Verás este mensaje en la terminal:
```
Sesión AWS expirada. Abre otra terminal y ejecuta:
aws sso login --profile bedrock
```

Abre otra terminal y ejecuta:
```bash
aws sso login --profile bedrock
```

Aprueba en el browser. El agente retoma automáticamente — no lo reinicies.

### Verificar que todo está OK

```bash
python setup_sso.py --verify
```

Debe mostrar todo `✓`. Si algo falla, revisa que el venv esté activo.

### Cambiar entre modo Bedrock y API directa

```bash
# Modo Bedrock (Amazon Bedrock via SSO) — default
export AWS_PROFILE=bedrock

# Modo API directa (Anthropic API key)
export LLM_BACKEND=anthropic
```

### Para el equipo — primera vez en una máquina nueva

```bash
# 1. Clonar el proyecto
git clone [repo-dev-agent]
cd dev-agent

# 2. Crear entorno virtual e instalar dependencias
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 3. Copiar configuración local
cp config.example.py config_local.py
# Edita config_local.py con tus rutas de repos Go

# 4. Configurar credenciales AWS SSO (una sola vez)
python setup_sso.py
# Completa el login en el browser

# 5. Verificar
python setup_sso.py --verify
# Debe mostrar todo ✓

# 6. Iniciar el agente
python main.py
```

---

## Autenticación

Este workspace usa Amazon Bedrock vía SSO — no necesitas
una API key de Anthropic.

Configura tu perfil SSO una sola vez:
```bash
python setup_sso.py
python setup_sso.py --verify
```
Para más detalles ver ONBOARDING.md.

El agente renueva la sesión automáticamente (botocore renueva las credenciales
antes de que expiren, leyendo el token SSO del cache local).
Si ves "Sesión expirada" en la terminal, ejecuta en otra ventana:
```bash
aws sso login --profile bedrock
```

### Selección de backend

| Quieres… | Haz… |
|----------|------|
| Bedrock con el perfil correcto | `export AWS_PROFILE=bedrock` |
| API directa de Anthropic | `export LLM_BACKEND=anthropic` |

> ⚠️ Si tu shell ya exporta `AWS_PROFILE` (p.ej. `default`), el agente asumirá
> Bedrock automáticamente con ese perfil. Usa las variables de arriba para
> controlarlo explícitamente.

Los model IDs de `config.py` (`claude-opus-4-8`, …) reciben el prefijo
regional `us.anthropic.` automáticamente en modo Bedrock (configurable con
`BEDROCK_MODEL_PREFIX`); no hay que duplicarlos.

---

## Uso

```bash
python main.py                                  # modo interactivo (español)
python main.py "analiza los riesgos del repo <alias>"
python main.py --no-llm-planner "genera reporte de infraestructura"  # routing offline
```

### Ejemplos de pedidos
```
> Analiza los riesgos del flujo de autorización
> ¿Por qué dos servicios usan versiones distintas del módulo core?
> Genera el reporte de infraestructura del repo <alias>
> Revisa que el nuevo código en service/mi_handler.go cumpla con PCI-DSS
> Traza el flujo de una transacción de reverso
```

---

## Arquitectura

```
main.py → orchestrator (planner + state) → agente(s) especializado(s)
                                              ↓ usan
                                    tools/ (filesystem, git, analysis, rag)
                                    knowledge/ (base de conocimiento)
                                    memory/ (sesión + persistente)
escritura en repos → orchestrator/approval_gate.py (diff + confirmación)
```

- **agents/** — architecture, development, pr_review, debug, report, meta (Nivel 6).
- **orchestrator/** — planificación, estado, gate de aprobación.
- **tools/** — funciones que usan los agentes (lectura, escritura, git, análisis, RAG).
- **knowledge/** — `extracted/` (del código real), `professional/` (dominio), `team/` (ADRs).
- **memory/** — memoria de sesión y persistente (Niveles 4-5 en stub).
- **evaluator/** — evaluación y auto-mejora (Niveles 5-6 en stub).

---

## Niveles de autonomía

| Nivel | Flags | Comportamiento |
|-------|-------|----------------|
| **3** (hoy) | `AUTONOMY_LEVEL=3`, `AUTO_COMMIT=False` | Toda escritura pasa por aprobación manual. |
| **4** | `RAG_ENABLED=True` | Búsqueda semántica del código (ChromaDB). |
| **5** | `EVALUATOR_ENABLED=True`, `RUN_TESTS_AFTER_CHANGE=True` | Auto-aprueba si los tests Go pasan. |
| **6** | `META_AGENT_ENABLED=True`, `PROMPT_OPTIMIZATION_ENABLED=True` | El sistema mejora sus propios prompts. |

Activar un nivel superior es cambiar **configuración**, no código.

---

## Reglas de seguridad
- El agente **nunca** escribe en los repos sin tu aprobación (gate con diff).
- **Los commits los haces tú** (hasta Nivel 5).
- Nunca se hardcodean secretos ni se loggean datos de tarjeta (PCI-DSS).
- La knowledge base cita `archivo:línea` real; `[NO ENCONTRADO EN CÓDIGO]` = sin evidencia.
