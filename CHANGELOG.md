# Cambios

## Sin publicar — lote 5 (router)

Nada mueve la línea base de prompts (`prompt_hash`).

- `--agent` (ka, bridge) con una consulta de información simple usa el perfil breve (razón
  `override --agent · consulta breve`); `--completo`, `--debug` y "en detalle" lo apagan. El router
  sigue siendo `forced`; flags, claves de `--json-out`, exit codes y `--read-only` no cambian.
- Regla local `inherit_repo` (antes de `ambiguous`): un seguimiento corto cuya única referencia a
  código es un alias de `REPOS` ("¿y en el repo hsm?") tras una consulta útil de architecture con
  citas repite el análisis sobre ese repo sin planner (confianza media, `repo <alias>` en la línea
  de decisión). Sin turno previo pide precisar; tras pr_review/development o sin citas va al planner.
- 7 casos golden nuevos (60 en total) con `repos` por caso en `routing_cases.json`.

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
