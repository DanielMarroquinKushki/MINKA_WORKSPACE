# Cambios

## 0.2.5 — paso 12b (medidor pasivo: medir antes de corregir)

Nada mueve la línea base de prompts (`prompt_hash` aabeff39c4ee / a447c50efa60). Todo es conteo y
lectura local: ningún cambio toca el mensaje al modelo ni la respuesta mostrada.

### Parte 1 (commit `2554e19`)
- Símbolo → cita más cercana (`citation_check._assign_symbols`): en "`A` (x.go:10) y `B` (y.go:20)" `A`
  no se busca en y.go; los rangos `a-b` usan el final del rango. Caso de calibración "sender.go:68" sin
  marca falsa.
- Datos en disco con 0600/0700 (`metrics.append_private`, `write_private`, también `agent_runs.jsonl`);
  `doctor` avisa "Permisos de datos" (no bloquea).
- Rotación de `output/metrics/runs/<día>.jsonl` a `METRICS_KEEP_DAYS` (90), anunciada una vez por proceso;
  nunca toca `baseline_v3.json`. `stats --purge` (simula; `--yes` borra).
- Contadores nuevos en el registro: `detalle_views`, `branches_off_base`, `budget_gate`, `estimate_shown`,
  `profile`.
- `/bien [nota]` y `/mal [agente] [nota]` escriben `evals/response_feedback.jsonl` (0600; ts, run_id,
  router, plan, veredicto, nota redactada ≤80; nunca pedido, respuesta ni historial). `/status` muestra el
  conteo de la sesión.
- `stats --freeze-baseline [--force]`: congela `output/metrics/baseline_v3.json` con las categorías de
  salud del rango 2026-11-05..2026-11-11 (referencia previa al paso 12, no una meta).

### Parte 2
- **Ausencias, solo conteo** (`core/orchestrator/absence_check.py`): tras MOSTRAR la respuesta, cuenta
  "no existe", "no hay" y `[NO ENCONTRADO` (`absences_said`); del patrón estricto
  "no (existe|hay) (un|una|el|la)? `X`" / "[NO ENCONTRADO: X]" toma X y lo busca con
  `git grep -q -w -F` en el árbol checkouteado de los repos que la misma línea menciona (alias o carpeta;
  si no menciona ninguno, todos los de REPOS). Tope de 1 s y 3 nombres por corrida. Estados: refutada
  (`absences_refuted`), confirmada, no evaluable (`absences_unclear`: sin backticks, fuera del tope, git
  falló). El registro se completa reescribiendo su línea (`metrics.amend_run_record`). Nunca marca la
  respuesta ni avisa al modelo; `stats` y el resumen de corrida (stderr) muestran los tres contadores.
- `minka stats --salud [--semana AAAA-MM-DD] [--crudo]` (`core/health.py`): costo mediano (info_direct,
  llm/1, llm/2+), caché mediana, % citas no encontradas, % símbolo lejano/ausente (informativo),
  % ausencias refutadas (sobre las evaluables), % interrumpidas, % vueltas con tope, /bien sobre
  /bien+/mal (de `response_feedback.jsonl`), % cierres con oferta. Columnas: semana ISO del ancla, las 4
  anteriores y la base v3 si existe. El n SIEMPRE visible; con n < `HEALTH_MIN_N` (20) por categoría:
  "sin datos suficientes (n=K)". `--crudo` muestra la cifra igual con el aviso "muestra pequeña".
- `doctor`: cuatro chequeos no bloqueantes sobre los últimos 7 días: caché mediana < `HEALTH_CACHE_MIN`
  (0.80), citas no encontradas > `HEALTH_CITATIONS_MISSING_MAX` (0.05), ausencias refutadas >
  `HEALTH_ABSENCE_REFUTED_MAX` (0.0), costo mediano +`HEALTH_COST_RISE_PCT` (25 %) contra los 7 días
  anteriores. Con n < `HEALTH_MIN_N` dice "sin datos suficientes" y nunca alerta.
- `stats --purge --que metricas|logs|feedback|todo [--before AAAA-MM-DD] [--yes]`: `--que` obligatorio
  (exit 2 sin él); `--before` aplica a lo elegido (metricas: archivos de días anteriores; logs y feedback:
  líneas con `ts` anterior, el archivo queda); simula por defecto; `baseline_v3.json` nunca entra.
- `docs/ejemplos/com.minka.salud.plist`: plist de launchd de EJEMPLO (no instalado) que corre
  `minka stats --salud --crudo` a las 09:00 y solo imprime.

### Límites
- Muestra pequeña: semana 45 con 125 corridas, 46 con 18, 47 con 2 al cierre de esta versión. Casi todas
  las celdas de `--salud` dicen "sin datos suficientes" hasta acumular 20 por categoría.
- Sobre lo guardado (vistas previas de 500 caracteres) solo 10 de 112 negaciones eran evaluables con el
  patrón estricto; con él 1 queda refutada. Un patrón laxo (backtick en la misma oración) daba 6, pero
  arrastra alias y nombres de archivo como "ausencias". La precisión de las refutadas no está verificada
  a mano.
- `/bien` y `/mal` etiquetan respuestas, pero hay un solo usuario: el cociente mide a una persona.
- La comprobación de ausencias mira solo la rama checkouteada de cada repo (`git grep` sin tree-ish): si
  el repo está en una rama de trabajo, "existe" se refiere a esa rama (ver "Búsqueda sobre").
- Todo probado sin red (repos git temporales, agentes falsos, fechas fijas). El plist no se instaló.

## 0.2.4 — paso 12 (que minka no afirme de más)

Nada mueve la línea base de prompts (`prompt_hash` aabeff39c4ee / a447c50efa60). Nota de historia:
la parte 2 se publicó dentro del commit `0285a45`, titulado como la parte 1.

### Parte 1
- Ids `R-nn` contra el `risk_catalog.md` del proyecto: existentes → `(doc: risk_catalog.md)`;
  inexistentes → `⚠ id inexistente en el catálogo: R-14`. Sin catálogo no se marca nada. Contador
  `risks_unsupported` en el registro, `aggregate` y `minka stats` ("riesgos sin catálogo").
- Duraciones (`300 s`, `5 min`, `30s`, `1h`, `3 días`…) con procedencia en tres niveles: en el repo →
  sin marca (código); solo en `knowledge/extracted|learning` → `(solo knowledge: 300 s →
  archivo:línea)`; en ninguno → `⚠ sin fuente: 1800 s`. Líneas con "reunión" excluidas.
  `Evidence.docs` pasa de texto a lista (archivo, línea, texto) y `load_project_docs` devuelve esa lista.
- Saludo + texto ("hola minka, estoy de vuelta", "buenas, retomemos lo de ayer"): se enruta lo que
  sigue al saludo; sin señal de tarea → `ambiguous` ($0). "hola" sigue en `small_talk`; "hola, ¿dónde…?"
  en `info_direct`. El caso golden `buenas_tardes_equipo` pasa de `no_plan` (planner) a `ambiguous`.
- `/mal`: el uso muestra `[agente_esperado] [nota]` literalmente (Rich los tomaba como markup).
- `ConsistencyMemory` compara solo entre corridas distintas (clave pregunta + `run_id`): los pasos de
  un mismo plan ya no producen "⚠ inconsistente con la corrida anterior".
- Estimado previo por clave (ruta, nº de pasos) (`metrics.route_key`, `route_median_counts(by_steps=True)`):
  un plan de 2 pasos no se estima con la mediana de los de 1; con menos de `ESTIMATE_MIN_RUNS`, `sin estimado`.
- `quality_evals.score` quita las marcas del verificador (`claim_check.strip_marks`) antes de puntuar.

### Parte 2
- Símbolo cerca de la cita (`citation_check`, reutiliza `citations._check_symbols`, `SYMBOL_WINDOW=5`):
  símbolo = identificador en backticks de la misma línea con mayúscula, `_` o `.` (las minúsculas sueltas
  no cuentan). Estados `ok`, `⚠ símbolo a N líneas`, `⚠ símbolo no está en el archivo`. Una línea con
  varios símbolos vale si alguno está cerca. Contadores nuevos `citations_ok`, `citations_symbol_far`,
  `citations_symbol_absent` junto a los antiguos `citations_total`/`citations_missing` (registro,
  `aggregate`, resumen de corrida y `stats`).
- Antigüedad del contexto: `team/session_log.md` y `team/decisions_log.md` entran SOLO como evidencia
  del verificador (índice fecha → archivo), nunca en el prompt ni en `AGENT_KNOWLEDGE`.
  `CONTEXT_STALE_DAYS=30`: fecha o ADR más viejo → `(entrada del <fecha>; verifica en el código)`;
  reciente → `(doc: session_log)`. Las fechas del contexto y la fecha dentro de `ADR-nnn (fecha)` ya
  no salen "⚠ sin respaldo".
- Rama por repo (`core/tools/git/branches.py`, solo lectura: `rev-parse --abbrev-ref HEAD` y
  `rev-list --left-right --count base...HEAD`, base master → main → origin/*): foto al inicio de cada
  corrida y en `/status` ("Ramas"); en el resumen de corrida (stderr) solo si algún repo no está en su
  base. Si la respuesta dice "no existe", "no hay" o `[NO ENCONTRADO` y algún repo no está en su base,
  se añade `_Búsqueda sobre: pos@rama (+N sobre master), hsm@rama (M detrás)_` (solo para mostrar).
  Nunca va al mensaje del modelo.

### Límites
- Mediciones del mes sobre vistas previas de 500 caracteres ya anotadas (`agent_runs.jsonl`), no sobre
  respuestas completas: 26 de 322 citas cambiarían de estado (18 lejano, 8 ausente); 0 marcas nuevas
  de duración y 6 `R-nn` todos existentes. Ids `R-nn` y duraciones sin ejercitar en respuestas completas.
- La foto de ramas cuesta ~430 ms por corrida con 4 repos (git local, medido en una máquina).
- "Citas verificadas" cuenta ahora cosas distintas: las líneas base anteriores (28/0, 49/1, 37/0) solo son
  comparables con el contador antiguo `citations_total`/`citations_missing`, que se conserva.
- Todo probado sin red (repos git temporales, agentes y planner falsos, fechas fijas).

## 0.2.3 — lote 5 (router)

Nada mueve la línea base de prompts (`prompt_hash`).

- `--agent` (ka, bridge) con una consulta de información simple usa el perfil breve (razón
  `override --agent · consulta breve`); `--completo`, `--debug` y "en detalle" lo apagan. El router
  sigue siendo `forced`; flags, claves de `--json-out`, exit codes y `--read-only` no cambian.
- Regla local `inherit_repo` (antes de `ambiguous`): un seguimiento corto cuya única referencia a
  código es un alias de `REPOS` ("¿y en el repo hsm?") tras una consulta útil de architecture con
  citas repite el análisis sobre ese repo sin planner (confianza media, `repo <alias>` en la línea
  de decisión). Sin turno previo pide precisar; tras pr_review/development o sin citas va al planner.
- 7 casos golden nuevos (60 en total) con `repos` por caso en `routing_cases.json`.
- Medido una vez por par (`stats --source lote5_before/after`): seguimiento entre repos $0.284 → $0.078
  (5 → 2 vueltas); `ka` con consulta simple $0.280 → $0.155 (36 → 12 líneas).
- Tope de sesión y ráfaga (`core/orchestrator/budget.py`): `Orchestrator.session_usd` suma el costo
  de cada corrida al cerrarla (planner e interrumpidas incluidos). Antes de lanzar un agente se
  levanta `BudgetGate(kind, usd, n)` si la sesión alcanzó `SESSION_MAX_USD` (5.0) o si hay ráfaga
  (`BURST_N`=3 corridas con agente seguidas a menos de `BURST_GAP_SECONDS`=5 s del fin de la
  anterior). El orquestador no lee stdin: en el REPL con TTY `main.py` pregunta ("sí" sube el tope
  un escalón o reinicia la ráfaga; "no" descarta); sin TTY o `MINKA_SOURCE=bridge`, línea y exit 3.
  Las capas de $0 nunca frenan; `evals quality` y el bridge están exentos. `/status` muestra el $ de
  la sesión. `DAILY_MAX_USD` (20) solo avisa en `stats` y `doctor` (sin `eval_quality`).
- El costo del planner LLM ahora forma parte de `cost_usd` de la corrida (`per_agent` lleva una
  entrada `planner`): las medianas de la ruta `llm` suben respecto al histórico.
- Estimado previo: con router `llm` o plan de 2+ pasos la línea de decisión añade `estimado ~$X
  (mediana de N corridas)` desde `metrics.route_median_counts` (mínimo `ESTIMATE_MIN_RUNS`=5; si no,
  `sin estimado`). Confirmación (`BudgetGate` kind `estimado`) solo si supera `ESTIMATE_CONFIRM_USD`
  (0.50) o rompería el tope; tras el "sí" se relanza reutilizando la decisión, sin segunda llamada
  al planner.
- `/mal [agente_esperado] [nota ≤80]` en el REPL: escribe `projects/<p>/evals/routing_feedback.jsonl`
  (0600, carpeta 0700) con ts, run_id, project, router, reason, confidence, plan, pedido normalizado y
  redactado (≤120 caracteres; si no, sha256), expected y note. Nunca respuestas, tools, historial ni
  reasoning. `minka evals routing --from-feedback` genera `evals/routing_cases.draft.json` sin LLM;
  `routing_evals.info_cases` lee además `projects/<p>/evals/routing_cases.json` si existe. Esos
  archivos contienen texto de pedidos: no versionar.
- Límites: freno, estimado y `/mal` probados solo sin red (agentes, planner, reloj y `Confirm.ask`
  falsos); la pregunta en terminal real y el estimado con medianas reales no se ejecutaron.

## 0.2.2 — lote 6 (operación)

Nada mueve la línea base de prompts (`prompt_hash`).

### 6a — Hermeticidad, `--version`, resultado parcial, CI
- La suite de tests no ve el entorno del dev: `conftest.py` borra `MINKA_*`, `AWS_*` y
  `ANTHROPIC_API_KEY`, apunta `HOME` y `MINKA_HOME` a temporales y los restaura al final.
- `minka --version`: versión, commit y `requested_revision` (de `direct_url.json`); dice si la
  instalación es editable o desde un wheel local.
- Fallos de red, timeout, throttling y credenciales (SDK de anthropic y botocore) en el bucle del
  agente devuelven el texto de las vueltas completadas más `⚠ corrida interrumpida: <tipo>, N vueltas
  completadas`; SSO vencido sugiere `aws sso login --profile <perfil>` sin traceback. El parcial no
  es turno útil, corta el plan, no cuenta como acierto en `evals quality` y se cuenta en `stats`
  (`interrumpidas`, `respuestas vacías`). `TypeError` y Ctrl-C siguen subiendo; sin reintentos extra.
  La CLI sale con 1 y una línea; el REPL sigue vivo.
- `.github/workflows/ci.yml`: pytest con lock (3.12/3.13), ruff, escaneo de secretos con
  `security.scan_text` (permitidos solo en `tests/`), `prompt_hash` del motor contra
  `baseline/prompt_hash_motor.txt`, build + instalación aislada, versión == tag en push de tag,
  resolución sin lock. Sin secrets, `permissions: contents: read`.

### 6b — Release/rollback, `minka trace`, retención, docs de operación
- `scripts/release.sh vX.Y.Z [--dry-run]`: verifica (status limpio, versión, README, tag libre,
  `uv lock --check`, CI por `gh` si existe), construye, crea el tag anotado local, instala desde el
  tag en un `UV_TOOL_DIR` temporal y compara `--version`; solo imprime los comandos de push.
  `scripts/rollback.sh vANTERIOR` reinstala y comprueba `--version` y `doctor` sin tocar `MINKA_HOME`.
  Probados contra un repo git local (`scripts/verify_release_scripts.sh`, `tests/test_release_scripts.py`).
- `minka trace` (apagado por defecto; `TRACE_ENABLED` o `--trace`): un JSONL por corrida en
  `output/trace/<día>/<run_id>.jsonl` (0700/0600) con eventos `route` (reglas locales descartadas),
  `turn`, `tool` (args filtrados, tamaño del resultado, bloqueada) y `end`; nunca texto del modelo
  ni contenido de tools; rotación `TRACE_KEEP_DAYS` (7) anunciada; `trace <run_id>`, `--last`, `--clear`.
- Retención: `agent_runs.jsonl` rota a `AGENT_LOG_KEEP_DAYS` (30) con aviso y copia `.1`, sin tocar
  métricas. `minka doctor` muestra tamaños (`.chromadb`, logs, trace, runs) y avisa sobre
  `DOCTOR_SIZE_WARN_MB` (500).
- `docs/OPERACION.md`: qué se guarda y por cuánto, costo, SSO, `MINKA_HOME`, publicar/revertir, no verificado.

## 0.2.1 — 2026-11-12

Tres lotes sobre la base 0.1.0 (REPL con pegado, inferencia de proyecto, `knowledge init`). Ningún cambio
mueve la línea base de prompts (`prompt_hash`): todo vive en la capa de herramientas, en el mensaje `user`
o fuera del modelo.

### Lote 1 — Seguridad en la capa de herramientas
- Exclusión por tipo de archivo (`EXCLUDE_PATTERNS`, ampliable por proyecto) en lectura, listado, grep,
  indexado, búsqueda semántica y diff; mensaje "excluido por política de seguridad" sin contenido.
- Redacción de secretos de alta confianza antes del modelo y en `agent_runs.jsonl` (claves de acceso,
  llaves privadas, JWT, Bearer, PAN válido por Luhn); hex, KCV y alias no se tocan. `SECRET_REDACTION_ENABLED`.
- `minka knowledge scan` y aviso en `minka doctor`; valores de prueba y marcas `<!-- ejemplo -->` cuentan aparte.
- Métricas sin ruta del home (`cwd` como `~/…`); `reasoning` del planner redactado. Hook de pre-commit
  versionado en `.githooks/` (no activo por defecto). Instalación recomendada desde tag.

### Lote 2 — Voz, decisiones y verificador
- `VOICE` cercana/directa en el mensaje `user` (apertura breve, riesgos 🔴🟠🟡, una recomendación con cita);
  `--completo`, `--debug` y `--plain` la apagan.
- Historial de decisiones (ADR) y alcance del ticket como contexto determinista a $0, con reglas de uso;
  referencias `ADR-nnn` verificadas contra el historial.
- Verificador de afirmaciones: nombres de herramientas excluidos, "inferido sin herramienta", cadenas de
  runtime, inconsistencias entre corridas de la misma pregunta; contadores en `minka stats`.
- Planes de varios pasos: solo se imprime el último paso; `/detalle` muestra la investigación. Perfiles
  exploratorio y decisión; tope de vueltas con respuesta final.

### Lote 3 — Calidad medida
- `minka evals quality`: golden set por proyecto con verificación determinista (existence, location, count,
  absence, consistency con anchor) sin juez LLM; validación a $0 contra el código vivo (`repo_commit`,
  símbolos definidos); tope de gasto proyectado con medianas reales; `--refresh`, `--draft`, `--json-out`.
- Las corridas de evaluación (`MINKA_SOURCE=eval_quality`) no entran en los agregados de `minka stats`.

### Dependencias
- Topes superiores en las dependencias críticas (`anthropic<3`, `boto3<2`, `botocore<2`, `pydantic<3`,
  `typer<1`, `rich<16`, `prompt-toolkit<4`, `chromadb<2`): `uv tool install` desde git no aplica `uv.lock`.
