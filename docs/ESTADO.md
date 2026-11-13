# Estado de minka — qué es, cómo funciona y qué se ha comprobado

Fecha: 2026-11-12 · versión del paquete 0.2.1 (tag `v0.2.1`, HEAD `b1c9007`) · suite 611 tests (63 archivos).
Todo lo afirmado aquí se verifica en el código (`archivo:línea`), en los tests o en los informes de
medición (`escaner_final.md`, `escaner_cierre.md`, `escaner_separacion.md`). Lo que no se pudo
comprobar está marcado **no verificado**. Los ejemplos usan un "proyecto de ejemplo" con repos
ficticios `svc-api` y `svc-core`.

## 1. Qué es y para quién

minka es un asistente de línea de comandos que responde preguntas sobre el código de un proyecto,
revisa cambios y redacta reportes, usando modelos de Anthropic (vía Bedrock o API directa) y un
conjunto fijo de agentes con herramientas de solo lectura sobre los repos declarados. Antes de gastar
tokens decide con reglas deterministas quién atiende el pedido; después de responder verifica que las
citas `archivo:línea` y los identificadores existan en los repos. Está pensado para un desarrollador
que consulta y revisa código con IA desde la terminal, dentro de un equipo que comparte el contexto
del proyecto (knowledge, decisiones, bitácora). No es un agente autónomo: escribir en los repos pasa
siempre por confirmación humana (`core/orchestrator/approval_gate.py:1-4`).

## 2. Cómo funciona un pedido

```
 entrada                router ($0)                planner (LLM)         agentes + tools
 REPL / pegado   ──►  capas 1-4:             ──►  solo si no hubo   ──►  architecture, debug,
 argumento CLI         saludo, estado, meta,       decisión directa      development, pr_review,
 bridge (--read-only)  contexto vivo, ambiguo,     o hay varios pasos    report, meta
                       info_direct, herencia                                  │
                                                                              ▼
 memoria de sesión  ◄── formato de salida  ◄── verificador: citas  ◄── resultado por paso
 (solo RAM, /clear)     perfil + nota        archivo:línea y afirmaciones   (contexto completo
 + métricas sin texto   + investigación      (⚠ no existe / ⚠ sin respaldo)  al paso siguiente)
                        oculta (/detalle)
```

- **Entrada.** REPL con prompt_toolkit cuando hay TTY: Enter envía, Alt+Enter salto de línea, un
  pegado de 3+ líneas o 500+ caracteres se colapsa en `[Pegado #N +M líneas]` y se expande al
  enviarlo (`core/repl_input.py:1-8`). Sin TTY, `input()` de siempre. También `minka "pedido"` y el
  bridge de otro agente con `--read-only` y `MINKA_SOURCE=bridge`.
- **Router** (`core/orchestrator/router.py:1-11`): capa 1 respuestas a $0; capa 2 planner solo sobre el
  pedido actual (el historial no se enruta); capa 3 reglas duras por agente; capa 4 herencia o pedir
  precisión. El historial viaja aparte, rotulado como contexto.
- **Planner** (`core/orchestrator/planner.py`): LLM con esquema `Plan` (`steps` con `min_length=1`) y
  fallback por keywords en español (`planner.py:45`). Solo se llama cuando el router no decidió.
- **Agentes y tools**: cada agente ejecuta un bucle ReAct con tope de vueltas y de repeticiones
  (`core/agents/base_agent.py`). En planes de varios pasos el siguiente agente recibe el resultado
  completo del anterior, con tope `CONTEXT_NEXT_MAX_CHARS` (`core/orchestrator/state_manager.py`).
- **Verificador**: citas `archivo:línea` contra REPOS (`core/orchestrator/citation_check.py:1-10`) e
  identificadores, bloques de código y cifras contra pedido, paso previo, repos y knowledge
  (`core/orchestrator/claim_check.py:1-11`). Anota en el texto; el texto anotado es el que hereda la
  memoria.
- **Formato**: perfil de respuesta por ruta (`core/orchestrator/response_profile.py`), nota
  "respuesta acotada", y en planes de 2+ pasos solo se imprime el último paso; lo demás queda en
  `/detalle` (`core/orchestrator/orchestrator.py:186-201`, `main.py:123-152`).
- **Memoria**: historial único en RAM (`core/memory/conversation_memory.py:1-6`), contexto vivo del
  proyecto como primer bloque del mensaje user (`core/memory/live_context.py:1-10`); métricas por
  corrida sin tarea ni respuesta (`core/metrics.py`, `tests/test_run_metrics.py`).

**Rutas del router** (nombre que aparece en `minka stats` y en la línea de decisión):

| Ruta | Qué significa | Costo |
|---|---|---|
| `small_talk` | saludo; responde con la lista de agentes (`router.py:356`) | $0 |
| `state` | "¿en qué quedamos?" sin contexto vivo: estado de la sesión (`router.py:359`) | $0 |
| `meta` | pregunta sobre la propia herramienta (`router.py:362`) | $0 |
| `live_context` | "¿en qué quedamos?" con contexto vivo: lo responde el archivo del proyecto (`orchestrator.py:477`) | $0 |
| `ambiguous` | sin verbo, código ni pregunta clara: pide precisar (`router.py:387`, `ASK_AMBIGUOUS` :117) | $0 |
| `no_changes` | pide revisar un diff pero el repo no tiene cambios (`router.py:280`) | $0 |
| `review_direct` | pide revisar y hay cambios o rama citada: pr_review sin planner (`router.py:272`) | agente |
| `info_direct` | pregunta de información simple (sin verbo de escritura, fallo ni encadenado, y sin pedir reporte): architecture directo, perfil breve (`router.py:397`) | agente |
| `inherit` | pedido ambiguo con turno previo útil: hereda ese agente (`router.py:420`) | agente |
| `inherit_repo` | seguimiento corto cuya única referencia a código es un alias de `REPOS` ("¿y en el repo hsm?") tras una consulta útil de architecture con citas: repite el análisis sobre ese repo sin planner, confianza media (`router.is_repo_followup`) | agente |
| `followup_change` | "arregla eso" tras una consulta con citas: development directo (`router.py:380`) | agente |
| `forced` | `--agent` o bridge: salta el planner; perfil completo salvo consulta de información simple → breve, razón `override --agent · consulta breve` (`orchestrator._run_forced`) | agente |
| `llm` | el planner decidió el plan (uno o varios pasos) (`router.py:521`) | planner + agentes |
| `keyword` | planner por palabras clave (`--no-llm-planner` o LLM caído) | agentes |
| `no_plan` | el planner no devolvió pasos válidos: pregunta de información → architecture; si no, pide precisar (`router.py:496-503`) | $0 o agente |
| `resume` | corrida interrumpida que se reanuda (`orchestrator.py:511`) | agentes |

## 3. Componentes

**Agentes** (`src/minka/core/agents/`, 6 más el base): `architecture_agent` analiza código, patrones,
riesgos e interfaces (Opus); `development_agent` propone cambios y los escribe solo tras el
ApprovalGate (Opus); `debug_agent` diagnostica fallos (Opus); `pr_review_agent` revisa diffs y
checklist (Sonnet); `report_agent` redacta reportes sobre resultados previos (Haiku); `meta_agent`
evalúa la sesión (Opus, apagado por defecto `META_AGENT_ENABLED`). Modelos en `config.py:49-68`.

**Herramientas** (24 registradas, `core/tools/registry.py`): lectura (`read_file` con rango,
`scan_directory`, `list_files`, `grep_code`, `find_usages`, `list_repos`), análisis
(`detect_risks`, `detect_patterns`, `parse_interfaces`, `find_interface`, `map_dependencies`,
`check_core_version`, `check_version_drift`, `check_pci_compliance`, `validate_diff`), git de lectura
(`show_diff`, `git_log`, `current_branch`), escritura solo para development (`write_file`,
`apply_diff`, `create_branch`, `commit_change`, `create_pr_description`) y `semantic_search` (RAG).
Qué tool ve cada agente lo fija `AGENT_TOOLS` del proyecto (`projects/_template/config.py.example:57`);
6 tools son exclusivas de Go (`core/stacks.py:29-38`) y la de PCI exige `COMPLIANCE` con "pci"
(`stacks.py:39`). `list_repos` está siempre (`registry.py:481`).

**Knowledge**: `.md` por agente desde `knowledge/` del proyecto (`AGENT_KNOWLEDGE`) o los defaults del
stack (`core/knowledge/loader.py:49-61`); entra en el system prompt junto a la identidad del agente.

**Perfiles de respuesta** (`response_profile.py`): `breve` (info_direct, ~12 líneas con todas las
citas), `exploratorio` (planner con solo architecture: conclusión, hallazgos, riesgos, qué falta
decidir), `decision` (último paso de un plan de 2+ agentes: la misma forma y sin datos que no estén
en el paso previo), `reporte` (report_agent solo), `completo` (lo demás, `--completo`, `--debug`,
"en detalle"). El bloque va en el mensaje user, nunca en el system: no mueve la línea base.

**Caché de prompts**: system (tools + identidad + knowledge) con `cache_control`, TTL 1 h para
architecture y 5 min para el resto (`config.py:179-196`), más un marcador móvil en el último bloque
user de cada vuelta (`base_agent._mark_cache_tail`). **Historial único**: una entrada por turno,
recortada una vez, compartida por todos los agentes. **Contexto vivo**: `<proyecto>.md` + última
entrada real de la bitácora, tope 60 líneas / 750 tokens, aviso si tiene más de 7 días
(`config.py:144-148`). **Investigación oculta** y `/detalle` (sección 2). **ApprovalGate**: diff
coloreado y confirmación antes de escribir; `LINE_CAP` 150 líneas solo advierte
(`approval_gate.py:83`). **Topes**: 20 vueltas por agente y 8 para architecture con bloque de
"última vuelta" (`config.py:163`), 3 llamadas idénticas a una tool (`config.py:160`), `MAX_TOKENS`
8192, fix-loop con tope `FIX_LOOP_MAX_USD` 2.0 (`config.py:251`). No hay tope de costo por consulta
normal: lo controla el dev con `minka stats`.

**Seguridad en la capa de herramientas** (`core/security.py`, 0.2.1): `EXCLUDE_PATTERNS` (`.env*`,
`*.pem`, `*.key`, `*.p12`, `*.jks`, `id_rsa*`, `credentials`, `secrets*`, `.aws`, `.ssh`; ampliable con
`EXCLUDE_PATTERNS_EXTRA`) se aplica en `read_file`, `scan_directory`, `list_files`, `grep_code`,
indexador, `semantic_search`, `show_diff` y en la guardia central `resolve_repo_paths`; cada
resultado de tool pasa por `redact()` (claves AWS, llaves privadas, JWT, Bearer, PAN válido por Luhn →
`[REDACTED:TIPO]`; hex, KCV y alias no se tocan) antes de ir al modelo y antes de escribir
`agent_runs.jsonl`. Contadores `security_excluded` y `redactions` en métricas.

**Voz y decisiones** (0.2.1): `VOICE` cercana|directa en el mensaje user para breve, exploratorio,
decisión y reporte (`--completo`, `--debug`, `--plain` la apagan; `core/orchestrator/response_profile.py`).
Historial de decisiones (`knowledge/team/decisions_log.md`, entradas `## ADR-nnn — título` con
`**Fecha:**` y `**Estado:**`) y alcance del ticket como bloque user determinista a $0 (hasta
`CONTEXT_DECISIONS_MAX` entradas por términos, tope `CONTEXT_DECISIONS_MAX_TOKENS`;
`core/memory/decisions.py`); una decisión reemplazada se marca y no se usa como base.

**Verificador de afirmaciones** (`core/orchestrator/claim_check.py`): identificadores en backticks,
bloques de código y cifras contra pedido + pasos previos + repos (`git grep`) + knowledge ("(doc)");
referencias `ADR-nnn` contra el historial; nombres de herramientas excluidos y "inferido sin
herramienta" si no se ejecutó; cadenas con placeholders → "depende de runtime"; misma cita con
valores distintos entre corridas de la misma pregunta → "⚠ inconsistente con la corrida anterior"
(`ConsistencyMemory`, solo RAM).

## 4. Comandos y flags

| Comando / flag | Qué hace |
|---|---|
| `minka` | REPL. Comandos: `/status` (incluye $ de la sesión), `/clear`, `/detalle [N]`, `/mal [agente] [nota]`, `salir` |
| `minka "pedido"` | una consulta y sale |
| `--project P` | proyecto activo (si no: directorio actual, luego `DEFAULT_PROJECT`) |
| `--agent architecture\|report\|pr_review` | fuerza un agente de solo lectura y salta el planner; con una consulta de información simple sale breve |
| `--read-only` | si el router elige un agente que escribe, responde y sale con 2; implícito con `MINKA_SOURCE=bridge` |
| `--completo` | respuesta completa, sin perfiles acotados ni investigación oculta |
| `--debug` | muestra input y resultado de cada tool; también fuerza completo |
| `--plain` / `MINKA_PLAIN=1` | sin Markdown renderizado; imprime todo |
| `--quiet` | sin líneas de decisión ni tokens/costo (van a stderr) |
| `--json-out ruta` | resultado en JSON (para el bridge); imprime todo |
| `--level 3-6` | nivel de autonomía; 3 = ApprovalGate siempre |
| `--no-llm-planner` | planner por keywords |
| `--interactive/--no-interactive` | modo conversacional (default sí) |
| `--init` | copia `config_local.py` y `.env` al directorio de datos sin sobrescribir |
| `--new-project N [--repo a=ruta] [--stack] [--compliance] [--rag/--no-rag] [--yes]` | crea el proyecto, detecta el stack, corre el doctor |
| `--index-repos` / `--index-knowledge` | índice ChromaDB de código / de `.md` (extra `[rag]`) |
| `--evaluate`, `--optimize-prompt`, `--push-prompts` | nivel 5/6 y Langfuse; requieren flags de config y LLM |
| `minka init [--yes]` | onboarding guiado; nunca imprime lo que escribes |
| `minka doctor [--project]` | Python, uv, sesión, REPOS, chromadb, proyecto, contexto vivo, tamaños de datos (aviso sobre `DOCTOR_SIZE_WARN_MB`) |
| `minka --version` | versión, commit y `requested_revision` de la instalación (o "editable") |
| `--trace` / `TRACE_ENABLED` | traza local de la corrida en `output/trace/<día>/<run_id>.jsonl`; apagada por defecto |
| `minka trace [run_id \| --last \| --clear]` | lista, lee o borra las trazas locales |
| `minka stats [--day\|--month] [--project] [--agent] [--source]` | tokens, caché, costo, rutas, citas, afirmaciones, `prompt_hash` |
| `minka evals routing [--runs 1-5] [--json-out] [--from-feedback]` | mide el planner con los casos `eval=info` de `core/evals/routing_cases.json` (60 casos golden en total) más `projects/<p>/evals/routing_cases.json` si existe; cuesta tokens. `--from-feedback` no llama al LLM: convierte `evals/routing_feedback.jsonl` (`/mal`) en `evals/routing_cases.draft.json` |
| `minka stats` / `minka doctor` (tope diario) | si el gasto del día (sin `eval_quality`) alcanza `DAILY_MAX_USD` (20) avisan; nunca bloquean |
| Freno de gasto (`SESSION_MAX_USD` 5.0, `BURST_N` 3, `BURST_GAP_SECONDS` 5, `ESTIMATE_CONFIRM_USD` 0.50, `ESTIMATE_MIN_RUNS` 5) | antes de lanzar un agente: tope de sesión (suma planner e interrumpidas), ráfaga (3 corridas con agente seguidas a <5 s del fin de la anterior) y estimado previo (router `llm` o 2+ pasos). REPL con TTY pregunta ("sí" sube un escalón / reinicia la ráfaga / acepta el estimado; "no" descarta); sin TTY o bridge: línea y exit 3. Las capas de $0, `evals quality` y el bridge no pasan por él |
| `minka knowledge check [--project]` | citas del knowledge contra REPOS; exit 1 si hay rotas |
| `minka knowledge init [--project] [--apply] [--yes]` | borrador determinista de knowledge; `--apply` nunca sobrescribe |
| `minka knowledge scan [--project]` | secretos en `knowledge/` (ruta:línea y tipo, nunca el valor); `TEST_VALUES` y marcas `<!-- ejemplo -->` cuentan aparte; exit 1 con fallos |
| `minka evals quality [--project] [--runs 1-5] [--max-usd] [--json-out] [--refresh <id>] [--draft] [--template]` | golden set con verificación determinista (existence, location, count, absence, consistency con anchor), sin juez LLM; tope de gasto proyectado con medianas reales; `MINKA_SOURCE=eval_quality` fuera de `stats` |

## 5. Configuración y datos

- **Paquete** (repo del motor, `src/minka/`): código, prompts por defecto
  (`projects/_template/prompts/`), knowledge genérico, casos de evals. **Datos del usuario**
  (`MINKA_HOME`, o `~/.minka/` si no está): `config_local.py`, `.env`, `projects/<p>/` (config,
  prompts, knowledge, evals), `active-context/`, `output/` (métricas, logs, borradores, diagramas) y
  `.chromadb/` (`config.py:31-35`).
- **config_local**: `LLM_BACKEND` (`bedrock` con `AWS_PROFILE`, o `anthropic` con `ANTHROPIC_API_KEY`
  en `.env`; `config.py:265-268`), `DEFAULT_PROJECT`, `REPOS_ROOT`, `REPOS_OVERRIDES`,
  `ACTIVE_CONTEXT_DIR`, topes y flags de la sección 3.
- **Config del proyecto** (`projects/<p>/config.py`): `STACK` (`go` por defecto si falta,
  `stacks.py:26`), `COMPLIANCE` (`["pci"]` opcional), `REPOS` (alias → ruta, relativa a `REPOS_ROOT`
  o absoluta), `RAG_ENABLED`, `AGENT_TOOLS`, `AGENT_KNOWLEDGE`, `PR_CHECKLIST`, `RISK_PATTERNS`,
  `CORE_MODULE_NAME`.
- **En disco**: métricas por corrida (números y nombres, `output/metrics/runs/<día>.jsonl`, sin
  retención automática), log de corridas de los agentes con la tarea y 500 caracteres del resultado
  (`output/logs/agent_runs.jsonl`; rota a `AGENT_LOG_KEEP_DAYS` = 30 con copia `.1` y aviso), traza
  opcional por corrida sin texto del modelo ni contenido de tools (`output/trace/`, 0700/0600, rota a
  `TRACE_KEEP_DAYS` = 7), estado de una corrida interrumpida, borradores de knowledge. Detalle y
  diagnóstico: `docs/OPERACION.md`.
  **Solo en memoria**: historial de la sesión, investigación oculta, contexto vivo (no entra en logs
  ni métricas, `live_context.py:9-10`).
- **Qué no poner en knowledge, contexto vivo ni prompts**: secretos, llaves, tokens, API keys, PAN o
  datos de tarjeta, credenciales de SSO, URLs con token. Todo eso viaja al modelo
  (`docs/ONBOARDING.md`, "Qué NO poner").

## 6. Alcance: qué hace hoy, con evidencia

| Capacidad | Evidencia | n |
|---|---|---|
| Pregunta simple con caché caliente a ~$0.10 | escaner_final B1: $0.105 mediana (0.084–0.115), cache 93–96 % | 5 |
| Caché de prompts | 94 % mediana en pregunta simple; 87 % del mes (escaner_final); medición del marcador móvil 85→94 % | 5 / 83 / 12 |
| Enrutado determinista | 5/5 simples → info_direct; 5/5 a $0 con la ruta esperada (escaner_final B2, B5); 53 casos golden en tests (`tests/test_routing_golden.py`) | 11 / 53 |
| Planner sin planes vacíos | empty_plan 0 %, acuerdo 78 %, estabilidad 92 % (escaner_final, 36 llamadas) | 36 |
| Historial único sin duplicar | tareas de 53–148 chars, 0 con historial antepuesto (escaner_final) | 10 |
| Citas verificadas y anotadas | 49 citas / 1 no encontrada y anotada (escaner_final B4); 37/0 (escaner_cierre); `tests/test_citation_check.py` | 15 + 6 resp. |
| Afirmaciones sin respaldo marcadas | `tests/test_report_guard.py`, `tests/test_hidden_investigation.py` (21 + 19 tests); medición de 1 corrida: 2 sin respaldo y 8 "(doc)" sobre 76 | 1 |
| Onboarding con repo ajeno | 11 pasos / 28 s en HOME temporal: build, install, init, new-project (python y go detectados), knowledge init, apply, index, doctor, check (escaner_cierre B) | 1 |
| Proyecto por directorio actual | "(por cwd)" en dos repos temporales (escaner_cierre) | 2 |
| Knowledge init sin fugas | 0 menciones de `.env`/`.pem` en el borrador generado (escaner_cierre) | 1 |
| Pegado multilínea en el REPL | `tests/test_repl_input.py` (11 tests, bracketed paste simulado) | tests |
| Investigación oculta y /detalle | `tests/test_hidden_investigation.py` | tests |
| Bridge solo lectura | corrida forced $0.352 visible en `stats --source bridge` (escaner_final B6) | 1 |
| Exclusión y redacción en todas las tools de lectura | `tests/test_security.py` (18 tests, repo temporal con los 13 tipos excluidos; cliente falso que obedece un comentario de inyección y la tool lo bloquea) | tests |
| Voz, decisiones y referencias ADR | `tests/test_voice_decisions.py` (17), medición directa vs cercana: 15 líneas, $0.50 / $0.21 (`stats --source voice_before/voice_after`) | tests · n=2 |
| Verificador: tools, inferido, runtime, inconsistencia | `tests/test_lote3_verificador.py` (16) | tests |
| Evals quality (golden set determinista) | `tests/test_quality_evals.py` (28); línea base `quality_v3`: 20 casos, 18 evaluables, 18 aciertos, 2 no evaluables, 0 inestables, `--runs 3`, $7.92 de $12 | medido n=3 |

## 7. Limitaciones

**(a) Comprobadas en el código**
- Un solo proveedor de modelos: Anthropic, por Bedrock (perfil SSO) o API directa
  (`config.py:265-268`). No hay otro proveedor ni modelos locales.
- Mensajes, reglas del router y keywords del planner en español (`router.py:117-122`,
  `planner.py:45-59`); 6 de 7 prompts de plantilla terminan con "Responde en español".
- Sesgo a Go: `STACK` ausente = `go` (`stacks.py:26`); 6 tools solo con Go (`stacks.py:29-38`); el
  borrador de knowledge solo soporta python y go (`core/knowledge/extractor.py:13,406`). Python y
  node tienen perfil (extensiones, comando de tests, carpetas a saltar; `stacks.py:93-110`) y los
  ejemplos de los schemas se traducen al stack (`registry.py:508-519`); node en RAG y en uso real:
  **no verificado**.
- Sin skills ni instrucciones cargables: 0 ocurrencias en `src/`. Lo configurable por proyecto es:
  prompts (`core/prompt_loader.py:32-36`), knowledge, qué tools ve cada agente y patrones de riesgo.
  No se pueden definir tools ni agentes nuevos sin tocar el paquete (`registry.py`, `core/agents/`).
- Sin ejecución de código del proyecto: el runner de tests existe (`core/evaluator/test_runner.py`)
  pero exige `EVALUATOR_ENABLED` y `RUN_TESTS_AFTER_CHANGE`, ambos `False` (`config.py:249,252`).
  Git: hay `git_log`, `show_diff`, `current_branch` y commit tras aprobación; no hay `blame`
  (0 ocurrencias).
- El verificador no demuestra verdad: comprueba que la ruta y la línea existan, y que identificadores
  y cifras aparezcan en repos, paso previo o knowledge (`citation_check.py:1-10`,
  `claim_check.py:1-11`). No lee dentro de bloques de código ni de literales entre comillas.
- Knowledge que puede quedar viejo con citas válidas: `knowledge check` distingue inexistente,
  ambigua, símbolo movido y símbolo ausente, pero el aviso por commit de origen
  (`fuente_desactualizada`, `docs/diseño-ciclo5-knowledge-init.md` §5) no está en el CLI.
- `knowledge init`: hace árbol, módulos, imports internos, README, manifiesto y git log con citas
  verificadas, excluye secretos y aborta si los detecta (`extractor.py:36-52`). No hace: node, resumen
  con modelo (`--with-model` no existe), estados ni riesgos.

**(b) Observadas en uso real**
- Reportes largos con relleno: 6 corridas tipo reporte costaron $2.67 (`stats --source report_before`,
  3 por `info_direct` y 3 por planner, una con 3 agentes a $1.53); las respuestas de 400–800 líneas
  se observaron el 2026-11-05 fuera de los informes versionados: **no reproducible desde los informes**.
  Motivó el perfil `decision` y la investigación oculta (`stats --source decision_after`: $0.52, 51 líneas visibles).
- Falsos positivos del verificador con campos documentados pero no literales en el código: observado
  el 2026-11-05 y mitigado con la marca "(doc)" y el grep sin mayúsculas
  (`tests/test_hidden_investigation.py::test_identificador_solo_en_knowledge_se_marca_doc`). Mes: 16 sin
  respaldo de 193 comprobadas, 12 "(doc)".
- Seguimiento entre repos caía al planner y costaba más: $0.297 y $0.339 frente a ~$0.10 de una
  simple (escaner_final B3, escaner_cierre C). Lote 5: `inherit_repo` lo resuelve sin planner;
  medido una vez: $0.078 y 2 vueltas frente a $0.284 y 5 vueltas (`stats --source lote5_after/before`).
- Delegación forzada (bridge, `--agent`) sin perfil breve: $0.352 (escaner_final B6). Lote 5: con
  consulta simple sale breve; medido una vez: $0.155 / 12 líneas frente a $0.280 / 36 líneas.
- Freno de gasto y estimado previo (lote 5): probados solo con agentes, planner y reloj falsos; la
  pregunta con TTY real y el estimado con medianas reales **no verificados** en uso. El costo del
  planner ahora entra en `cost_usd` de la corrida: las medianas de la ruta `llm` suben respecto al
  histórico anterior al lote 5.
- Primera consulta del día con caché fría: $0.410 y cache 66 % frente a $0.105 caliente
  (escaner_cierre C, n=1).
- Dos agentes seguidos repiten hallazgos: el plan architecture→report devolvía ambos textos; hoy se
  oculta el primero (sección 2). Cuantificación: **no verificado**.
- "id inconsistente entre corridas": **no verificado** (no se identificó a qué id se refiere en
  código ni informes).

**(c) No probadas**
- Más de un usuario o máquina (todas las mediciones son de un dev; el onboarding ajeno fue una
  simulación en HOME temporal). Muestras pequeñas: n entre 1 y 5 en casi todas las filas de la sección 6.
- API directa de Anthropic en uso real, node de punta a punta, fix-loop y development en uso real
  (0 corridas con fix-loop en el mes), Langfuse (`OBSERVABILITY_ENABLED`, opcional), `--evaluate`.

## 8. Seguridad y privacidad

- **Al modelo viaja**: identidad del agente + knowledge del proyecto (system), contexto vivo, historial
  de la sesión recortado, el pedido, y todo lo que las tools lean de los repos (archivos, grep). Nada
  de eso se filtra por contenido antes de enviarse, salvo en `knowledge init`.
- **Nunca se lee** en `knowledge init`: `.env*`, `*.pem`, `*.key`, `*.p12`, `*.jks`, `id_rsa*`,
  `credentials`, `secrets*`, `.aws/`, `vendor/`, `node_modules/`, `.venv/`, `dist/`, `build/`
  (`extractor.py:37-42`); escaneo de 7 patrones de secretos que aborta listando `ruta:línea` sin el
  valor (`extractor.py:45-53`). Desde 0.2.1 las tools de lectura de los agentes aplican la misma
  política (`core/security.py`, `tests/test_security.py`): `read_file` devuelve "excluido por política
  de seguridad" sin contenido, y los secretos de alta confianza que sí entran por un archivo
  permitido llegan redactados al modelo.
- **ApprovalGate**: ninguna escritura sin confirmación (nivel 3 por defecto, `config.py:126`).
  **Bridge**: `--read-only` y `MINKA_SOURCE=bridge` rechazan agentes que escriben (`orchestrator.py:528-532`).
- **Queda en disco**: métricas sin tarea ni respuesta (`tests/test_run_metrics.py` lo exige);
  `agent_runs.jsonl` guarda la tarea y 500 caracteres del resultado ya redactados, y
  `per_iteration[].tools` guarda nombre y rutas/rangos/patrones de cada tool, nunca su contenido;
  `cwd` se guarda como `~/…` y el `reasoning` del planner pasa por la redacción.
  Nada de la investigación oculta ni del historial se escribe.
- **Riesgos conocidos**: todo lo que esté en knowledge o contexto vivo viaja al modelo; `detect_risks`
  busca PAN en logs, llaves embebidas y `eval`/`exec` (`core/tools/analysis/risk_analyzer.py:17-72`)
  pero es heurístico; `init` nunca imprime valores, pero `config_local.py` y `.env` quedan en claro en
  el directorio de datos.

## 9. Costos reales medidos

| Escenario | Costo | Caché | n | Fuente |
|---|---|---|---|---|
| Pregunta simple, caché caliente | $0.105 mediana (0.084–0.115) | 93–96 % | 5 | escaner_final B1 |
| Misma pregunta dentro de una sesión | $0.099 | 97 % | 1 | escaner_cierre C |
| Pregunta simple, caché fría (1.ª del día) | $0.410 | 66 % | 1 | escaner_cierre C |
| Secuencia fija de 5 pedidos | $0.565 y $0.540 (antes $1.12 y $1.52) | 93 % | 2 (+2 antes) | escaner_final B3, escaner_cierre C |
| Consulta exploratoria (planner, solo architecture) | $0.42–0.47 antes; $0.34–0.65 con perfil (una con caché fría) | 90–93 % | 3 + 2 | `stats --source explore_before/after` |
| Plan de 2 pasos (architecture → report) | $0.52 | 82 % | 1 | `stats --source decision_after` |
| Pedidos tipo reporte sin perfil | $0.13 / $0.54 / $1.53 (este último, 3 agentes) | 90 % | 3 | `stats --source report_before` |
| Delegación forzada (bridge) | $0.352 | — | 1 | escaner_final B6 |
| `ka` con consulta simple: completo → breve (lote 5) | $0.280 → $0.155; 36 → 12 líneas; 6 → 4 vueltas | — | 1 + 1 | `stats --source lote5_before/after` |
| "¿y en el repo hsm?" tras consulta: planner → `inherit_repo` (lote 5) | $0.284 → $0.078; 5 → 2 vueltas | — | 1 + 1 | `stats --source lote5_before/after` |
| Mes completo del proyecto en uso | $26.92 en 135 corridas; p90 architecture $0.49 | 87 % | 135 | `stats --month` |

Tarifa de lista de Anthropic sobre tokens medidos (`core/pricing.py`); no es la factura del proveedor.

## 10. Calidad medida y cómo reproducirla

- `minka stats --month AAAA-MM`: corridas, tokens, caché, costo frente a baseline, rutas, vueltas con
  tope, cierres recortados, citas no encontradas / verificadas, afirmaciones sin respaldo, "(doc)",
  salidas cortadas y `prompt_hash`. Hoy: 677 citas verificadas y 17 no encontradas; 16 afirmaciones
  sin respaldo de 193.
- `minka evals routing --runs 3`: 53 casos, mide empty_plan, acuerdo y estabilidad (cuesta ~$0.19).
- `minka knowledge check --project P`: citas del knowledge; hoy 292 citas, 216 ok, 9 inexistentes.
- `minka knowledge scan --project P`: secretos en knowledge; en el proyecto en uso 0 fallos, 9 a
  revisar y 7 ejemplos por lista (valores de prueba de los manuales).
- `minka evals quality --project P --runs 3`: línea base **quality_v3** (2026-11-11): 20 casos
  (3 legacy, 4 existence, 1 location, 5 absence, 4 count, 3 consistency), 18 evaluables, 18 aciertos,
  2 no evaluables, 0 caducados, 0 inestables, $7.92 de $12. Límites: n=3 corridas; el set se ajustó
  entre la base (22 casos, 70,6 %) y v2/v3 (20 casos: se retiraron 2 existence); los `cons-*`
  usan anchor y los 12 casos añadidos el 11-nov están "sin revisar". Guarda de regresión: una
  corrida con tasa < 1,0 sobre los 18 evaluables, cualquier caducado sin revisar o inestable > 0
  obliga a revisar antes de cambiar prompts o tools.
- **Línea base**: `prompt_hash` = sha256 de prompts + schemas de tools + modelos
  (`core/baseline.py:32-41`). Igual hash ⇔ misma línea base; todos los cambios de los últimos ciclos
  la dejaron en el mismo valor (escaner_cierre, 59 corridas del mes lo llevan).
- Suite: `uv run pytest` → 611 tests (63 archivos); `ruff check` limpio.

## 11. Pendientes y decisiones abiertas

- Hechos en 0.2.1 (lote 4): tag `v0.2.1`; motor viejo del workspace retirado (1,6 GB de `.venv`,
  `core/`, tests, docs; respaldo en tar); stubs y módulos sin importadores retirados
  (`code_evaluator`, `agent_evaluator`, `embedder`, `episodic_memory`, `persistent_memory`);
  `RUN_TESTS_AFTER_CHANGE` y `GO_TEST_TIMEOUT*` fuera del config (ningún código los leía).
- Pendiente: decidir sobre `report_agent`, `test_runner` y `SessionMemory` con los 5 casos tipo
  reporte del set de oro (medición antes/después con tope).
- `fuente_desactualizada` en `knowledge check` y soporte node en `knowledge init` (diseño ciclo 5).
- Formato de ruff pendiente en 62 archivos (`ruff format --check`).
- Hechos en el lote 6 (sin publicar): suite hermética, `--version`, resultado parcial ante fallos de
  red/timeout/throttling/credenciales, CI en GitHub Actions (no ejecutado aún: cuota de Actions en
  repo privado **no verificada**), `scripts/release.sh` / `rollback.sh` probados contra un repo
  temporal, `minka trace`, retención de `agent_runs.jsonl`, tamaños en `doctor`, `docs/OPERACION.md`.
- Hechos en el lote 5 (sin publicar): `ka`/`--agent` breve con consulta simple, `inherit_repo`,
  tope de sesión y ráfaga (`BudgetGate`, exit 3 sin TTY), estimado previo en la línea de decisión,
  `/mal` + `evals routing --from-feedback`, aviso de tope diario en `stats`/`doctor`. Medición de
  los dos primeros con una muestra por par (sección 9); el resto solo con pruebas sin red.
- Ideas anotadas sin abrir: skills por proyecto, detección de contradicciones entre pasos de un plan.

## 12. Cómo usarlo bien

1. Una pregunta clara por mensaje, con el repo o archivo si lo conoces: entra por `info_direct` y
   cuesta ~$0.10; encadenar dos preguntas obliga al planner.
2. Para explorar un tema amplio deja que el planner decida; leerás conclusión, hallazgos y riesgos.
   Si quieres el análisis completo, pide "en detalle" o usa `--completo`.
3. Pide reporte solo cuando ya hubo análisis en la sesión: el reporte redacta sobre eso y no inventa.
4. Tras un plan de varios pasos, `/detalle` muestra la investigación; `/detalle N` la del pedido N.
5. Verifica en el código lo importante: `⚠ no existe` y `⚠ sin respaldo` marcan lo que no se pudo
   comprobar; "(doc)" indica que solo está en el knowledge.
6. Arranca el día con la pregunta más simple: la primera consulta paga la caché fría.
7. Mantén `active-context/<proyecto>.md` corto y sin secretos: viaja en cada consulta.
8. Revisa `minka stats` al cerrar; si aparece más de un `prompt_hash`, las comparaciones de costo no valen.
9. Si el router se equivocó, escribe `/mal architecture razón corta` justo después: queda en
   `evals/routing_feedback.jsonl` (texto de tu pedido: no lo versiones) y `evals routing
   --from-feedback` lo convierte en casos para revisar.
10. Si aparece el freno de gasto, lee la línea: el "sí" sube el tope otro escalón para toda la sesión.

## Afirmaciones y evidencia

| Afirmación | Evidencia | Estado |
|---|---|---|
| Router con rutas a $0 antes del planner | `router.py:1-11`, `escaner_final B5` (5/5 a $0) | verificado · medido n=5 |
| Pregunta simple ≈ $0.10 con caché caliente | escaner_final B1 | medido n=5 |
| Caché fría multiplica por ~4 | escaner_cierre C | medido n=1 |
| Citas se verifican y anotan | `citation_check.py`, `tests/test_citation_check.py`, B4 | verificado · medido n=15 |
| Afirmaciones se verifican contra repos, paso previo y knowledge | `claim_check.py`, 40 tests | verificado · medido n=1 |
| Solo se imprime el último paso; /detalle muestra el resto | `orchestrator.py:186-201`, `tests/test_hidden_investigation.py` | verificado · medido n=1 |
| Escrituras siempre con confirmación | `approval_gate.py:1-4`, `config.py:126` | verificado |
| Bridge solo lectura | `orchestrator.py:528-532`, B6 | verificado · medido n=1 |
| Línea base intacta en los últimos ciclos | `core/baseline.py`, escaner_cierre | verificado |
| Onboarding con repo ajeno en minutos | escaner_cierre B (28 s, 11 pasos) | medido n=1 |
| Soporte real de node | perfil en `stacks.py:105-110`; sin RAG ni knowledge init; sin medición | no verificado |
| Más de un usuario | — | no verificado |
| `read_file` excluye archivos sensibles | `core/security.py`, `tests/test_security.py` | verificado |
| Secretos redactados antes del modelo y del disco | `security.redact`, `tests/test_security.py` | verificado |
| Decisiones y alcance como contexto a $0 | `core/memory/decisions.py`, `tests/test_voice_decisions.py` | verificado |
| Golden set determinista reproducible | `quality_v3` (18/18, n=3, $7.92) | medido n=3 |
| Reportes con relleno antes del perfil `decision` | `stats --source report_before` ($2.67, 6 corridas) | observado · líneas no reproducibles |
| "id inconsistente entre corridas" | — | no verificado |
