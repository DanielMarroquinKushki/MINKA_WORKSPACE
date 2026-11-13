# Operación de minka

Guía para quien opera el motor en su máquina: qué deja en disco, qué mirar cuando el costo sube,
qué hacer cuando falla la sesión, cómo diagnosticar `MINKA_HOME`, cómo publicar y revertir una
versión, y qué NO está verificado. Todo lo que dice aquí tiene test o se puede reproducir con los
comandos indicados.

## 1. Qué se guarda en disco, quién lo escribe y por cuánto tiempo

Todo vive bajo `MINKA_HOME` (si no está definida, `~/.minka/`). El paquete nunca escribe fuera de
ahí salvo lo que pida una tool de escritura aprobada por el ApprovalGate (parches en los repos).

| Ruta (bajo `MINKA_HOME`) | Qué contiene | Quién lo escribe | Retención |
|---|---|---|---|
| `config_local.py`, `.env` | backend, perfil AWS, `DEFAULT_PROJECT`, `REPOS_ROOT` | el dev (`minka init` crea plantillas sin sobrescribir) | manual |
| `projects/<p>/` | config, prompts, knowledge, evals del proyecto | el dev; `knowledge init --apply` solo añade | manual |
| `active-context/<p>.md` | contexto vivo (ticket, pendientes) | el dev | manual |
| `projects/<p>/evals/routing_feedback.jsonl`, `routing_cases.draft.json` | `/mal`: run_id, router, plan, pedido normalizado y redactado (≤120 caracteres; si no, solo sha256), agente esperado y nota; nunca respuestas ni historial | `/mal` en el REPL; `evals routing --from-feedback` | manual. Archivos 0600, carpeta 0700. Contienen texto de pedidos: **no versionar** |
| `output/metrics/runs/<día>.jsonl` | una línea por corrida: tokens, costo, router, `prompt_hash`, contadores; nunca la tarea ni la respuesta | `Orchestrator._close_run_record` | sin rotación automática (son pequeñas; `minka stats --month` las lee) |
| `output/logs/agent_runs.jsonl` | una línea por corrida de agente: tarea y 500 caracteres del resultado **redactados**, usage | `BaseAgent._log` | `AGENT_LOG_KEEP_DAYS` (30). Al abrir la primera corrida del proceso se retiran las líneas viejas, se avisa cuántas y queda copia completa en `agent_runs.jsonl.1` |
| `output/trace/<día>/<run_id>.jsonl` | traza por corrida: `route`, `turn`, `tool`, `end`; nunca texto del modelo ni contenido de tools | `core/trace_log.py`, solo con `TRACE_ENABLED` o `--trace` | `TRACE_KEEP_DAYS` (7); la rotación se anuncia con una línea al abrir la corrida; `minka trace --clear` borra todo. Directorios 0700, archivos 0600 |
| `output/state/` | checkpoint de una corrida interrumpida para reanudar | `StateManager` | se borra al completar la corrida |
| `output/knowledge_drafts/`, `output/reports/`, `output/diagrams/` | borradores y salidas pedidas | tools de escritura tras aprobación | manual |
| `.chromadb/` | índice RAG (extra `[rag]`) | `--index-repos` / `--index-knowledge` | manual; se reconstruye con los mismos comandos |

Qué **no** se guarda: el historial de la sesión, la investigación oculta y el contexto vivo viven solo
en memoria. Ningún archivo bajo `output/` contiene secretos sin redactar: todo texto libre pasa por
`security.redact` antes de tocar disco (`agent_runs.jsonl`, `reasoning` del planner, trazas).

`minka doctor` muestra el tamaño de `.chromadb`, `logs`, `trace` y `runs` y avisa (no bloquea) si
alguno supera `DOCTOR_SIZE_WARN_MB` (500).

## 2. Si el costo se dispara

0. El REPL frena solo (lote 5): al llegar a `SESSION_MAX_USD` (5.0) o ante una ráfaga (`BURST_N`=3
   corridas con agente seguidas a menos de `BURST_GAP_SECONDS`=5 s del fin de la anterior, el caso
   del pegado partido en varias líneas) pregunta antes de lanzar el agente; "sí" sube el tope otro
   escalón o reinicia la ráfaga, "no" descarta el pedido. Sin TTY o con `MINKA_SOURCE=bridge` sale
   con 3 y una línea clara. Con router `llm` o plan de 2+ pasos la línea de decisión trae
   `estimado ~$X (mediana de N corridas)` y pide confirmación si supera `ESTIMATE_CONFIRM_USD`
   (0.50); con menos de `ESTIMATE_MIN_RUNS` corridas de esa ruta dice `sin estimado`. `/status`
   muestra el $ de la sesión; `stats` y `doctor` avisan al alcanzar `DAILY_MAX_USD` (20) sin bloquear.
1. `minka stats --day` (o `--month`). Mira primero **rutas del router**: `info_direct` es una
   llamada directa a architecture (barata); `llm` pasa por el planner y suele encadenar pasos.
   Si la proporción de `llm` subió, revisa qué pedidos lo provocan (`minka trace --last` con la
   traza encendida muestra `route` con las reglas locales descartadas).
2. **`prompt_hash`**: si en el rango aparece más de uno, alguien cambió prompts, tools o `MODELS`;
   las comparaciones de costo entre esas corridas no valen. La huella del motor sin proyecto está
   en `baseline/prompt_hash_motor.txt`.
3. **Caché**: `stats` muestra lectura/escritura de caché. Un prefijo que cambia en cada vuelta
   (knowledge editado a mitad de sesión, contexto vivo enorme) hace que la escritura supere a la
   lectura. Topes: `LIVE_CONTEXT_MAX_TOKENS`, `CONTEXT_NEXT_MAX_CHARS`.
4. **Vueltas por corrida**: `ARCH_MAX_ITERATIONS` (8) y `TOOL_REPEAT_LIMIT` (3). Con `--trace`,
   los eventos `turn` traen tokens y `stop_reason` por vuelta; los `tool`, el tamaño de cada
   resultado. Resultados de tools de miles de líneas son la causa habitual de vueltas caras.
5. `interrumpidas` y `respuestas vacías` en `stats`: corridas que cobraron sin entregar. Si crecen,
   el problema es red o sesión, no el modelo (sección 3).

## 3. Sesión SSO vencida y resultado parcial

- Antes de una tarea, con Bedrock, el orquestador comprueba la sesión; si venció devuelve un
  mensaje con el comando de renovación y no llama al modelo.
- Si la sesión vence **durante** la corrida, o hay un corte de red, timeout o throttling (tras los
  dos reintentos del SDK), el agente devuelve lo que ya tenía más la línea
  `⚠ corrida interrumpida: <red|timeout|throttling|credenciales|servicio>, N vueltas completadas`.
  Con `credenciales` añade `Renueva con aws sso login --profile <perfil>`. Sin traceback.
- Ese resultado **no** entra al historial como turno útil, corta el plan (no se encadenan pasos
  sobre una base rota), no cuenta como acierto en `evals quality` y se cuenta en `stats`. La CLI
  sale con 1; el REPL imprime la línea y sigue. El costo de las vueltas completadas sí se registra.
- `⚠ el modelo no devolvió texto`: respuesta vacía; mismo tratamiento.
- `TypeError` y otros errores de programación, y Ctrl-C, no se capturan: suben con traceback.

```bash
aws sso login --profile <perfil>     # luego repite el pedido
minka doctor                         # "Sesión AWS" muestra los minutos restantes del token
```

## 4. `MINKA_HOME` mal definida

Síntomas: `minka doctor` marca "Directorio de datos" en ✗ aunque configuraste todo; `Proyecto
activo` dice "sin DEFAULT_PROJECT"; `minka stats` responde "Sin corridas registradas" tras usarlo;
aparece una carpeta `~/.minka/` nueva.

```bash
echo "${MINKA_HOME:-<sin definir → ~/.minka>}"
minka doctor                 # la primera línea de "Directorio de datos" dice qué falta y dónde
ls "$MINKA_HOME"             # debe tener config_local.py y projects/
MINKA_HOME=/ruta/correcta minka doctor
```

Causas habituales: la variable está en un shell y no en otro (exportarla en el perfil del shell);
apunta al repo del motor en vez de al directorio de datos; termina en `/dev-agent/` o no según la
instalación. Los tests nunca usan tu `MINKA_HOME`: la suite la redirige a un temporal.

## 5. Publicar y revertir una versión

```bash
# publicar (solo local; el push lo haces tú)
scripts/release.sh v0.3.0            # exige: status limpio, pyproject == 0.3.0, README con @v0.3.0,
                                     # tag libre, uv lock --check, CI verde si hay gh (si no, avisa)
                                     # construye, crea el tag anotado, instala desde el tag en un
                                     # UV_TOOL_DIR temporal y compara `minka --version`
git push origin <rama> && git push origin v0.3.0    # lo imprime el script; nunca lo ejecuta

# revertir
scripts/rollback.sh v0.2.1           # reinstala desde el tag y comprueba --version y doctor;
                                     # no toca MINKA_HOME. MINKA_EXTRAS="[rag]" si usas RAG
scripts/release.sh v0.3.0 --dry-run  # solo verificaciones y build (lo corre el CI)
```

Ambos scripts se prueban con `scripts/verify_release_scripts.sh` contra un repositorio git local
temporal (`file://`), nunca contra `origin`; `tests/test_release_scripts.py` lo ejecuta en la suite.

## 6. Qué NO está verificado

- El workflow de CI (`.github/workflows/ci.yml`) no se ha ejecutado en GitHub: ni la sintaxis con
  `actionlint` (no instalado) ni la **cuota de GitHub Actions en un repositorio privado**.
- `release.sh` consulta el CI con `gh run list`; sin `gh` o sin sesión solo avisa. No se ha probado
  con un repositorio remoto real (la verificación usa `RELEASE_SKIP_GH=1`).
- `rollback.sh` se probó instalando desde `git+file://`; desde `git+https://` usa el mismo comando
  pero no se ejecutó aquí.
- La traza añade una escritura por evento cuando está encendida; apagada no toca disco (test), pero
  la latencia con traza encendida no se ha medido.
- La retención de `agent_runs.jsonl` reescribe el archivo completo una vez por proceso; con archivos
  de cientos de MB no se ha medido el tiempo.
- `python 3.14` corre la suite localmente; el CI declara 3.12 y 3.13 (las de `requires-python`).
- Freno de gasto, estimado y `/mal` (lote 5): probados con agentes, planner y reloj falsos y con
  `Confirm.ask` simulado. La pregunta en una terminal real, el estimado con medianas reales de
  `output/metrics/runs` y el borrador `--from-feedback` sobre un proyecto real no se ejecutaron.
- El costo del planner entra en `cost_usd` desde el lote 5: las medianas de la ruta `llm` no son
  comparables con las corridas anteriores (mismo `prompt_hash`, distinta contabilidad).
- Las dos mediciones del lote 5 (`ka` breve, `inherit_repo`) son de una muestra por par.
