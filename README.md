# kushki-workspace — dev-agent

Sistema multi-agente de IA para desarrollo de software.
Analiza, revisa y modifica código respetando los estándares
técnicos y culturales del equipo.

Escalable por niveles de autonomía (3 → 6) mediante flags de configuración.

---

## Instalación

```bash
uv tool install git+<url-del-repo>
```

> **Próximamente:** el empaquetado está en curso; este comando aún no funciona.
> Mientras tanto, desde un clon del repo: instala `requirements.txt` en un venv,
> copia `config.example.py` a `<DATA_DIR>/config_local.py` (ver `_app.py`), corre
> `python -m cincha.setup_sso` y luego `python -m cincha.main`.

> Nivel 4 (RAG): `pip install chromadb`, pon `RAG_ENABLED=True` y luego
> `python -m cincha.main --index-repos`.

---

## Uso diario

### Iniciar el agente

```bash
python -m cincha.main   # desde el clon del repo, con el venv activo y `PYTHONPATH=src` hasta que el paquete sea instalable
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
python -m cincha.setup_sso --verify
```

Debe mostrar todo `✓`. Si algo falla, revisa que el venv esté activo.

### Cambiar entre modo Bedrock y API directa

```bash
# Modo Bedrock (Amazon Bedrock via SSO) — default
export AWS_PROFILE=bedrock

# Modo API directa (Anthropic API key)
export LLM_BACKEND=anthropic
```

## Autenticación

Este workspace usa Amazon Bedrock vía SSO — no necesitas
una API key de Anthropic.

Configura tu perfil SSO una sola vez:
```bash
python -m cincha.setup_sso
python -m cincha.setup_sso --verify
```

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
python -m cincha.main                                  # modo interactivo (español)
python -m cincha.main "analiza los riesgos del repo <alias>"
python -m cincha.main --no-llm-planner "genera reporte de infraestructura"  # routing offline
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
