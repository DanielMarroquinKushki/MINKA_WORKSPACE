# Ciclo 5 — `minka knowledge init --project <p>`

Estado: diseño aprobado para guardar, pendiente de implementar.
Objetivo: generar el knowledge inicial de un proyecto (cualquier stack) a partir del código
real, con citas `archivo:línea` verificables, sin tocar lo existente y con el modelo como
paso opcional, acotado y confirmado.

## 1. Parte determinista ($0)

Módulo nuevo `core/knowledge/extractor.py`. Por cada repo de `config.REPOS`, según el perfil
de `core/stacks.py`:

| Qué | Cómo | Reutiliza |
|---|---|---|
| Árbol de carpetas | profundidad 3, conteo por extensión, saltando `profile.skip_dirs` | `stacks.code_files` |
| Módulos e imports internos | python: `ast` sobre `import`/`from`; go: `import "<módulo del go.mod>/…"`; node: `import … from './…'` y `require('./…')` | `stacks.profile().deps_file` |
| README y manifiesto | primeras 60 líneas del README; `go.mod` / `pyproject.toml` / `package.json` | — |
| Actividad reciente | `git log -n 20 --format=%h %cs %s` por repo | `indexer.git_head` |
| Verificación de citas | cada afirmación nace con `archivo:línea` del AST o del grep y pasa `RepoIndex.resolve` + chequeo de línea antes de escribirse | `RepoIndex`, `citations.CITATION_RE` |

Salida en el borrador:
- `_sources.json` con el schema de freshness (`extracted_at`, `sources.<repo>.commit`,
  `paths` = carpetas de módulos).
- `architecture.md` con secciones: Árbol, Módulos, Dependencias internas, Manifiestos,
  Actividad reciente. Solo hechos citables; nunca estados ("vigente", "cerrado") ni riesgos.

## 2. Parte con modelo (`--with-model`, opcional)

- Una llamada por repo con `structured.call_structured_result`, schema
  `{modulo: {resumen, responsabilidad, citas[]}}`, modelo `MODELS["report_agent"]` (Haiku).
- Entrada: el borrador determinista + las primeras 40 líneas de cada módulo. Nada más.
- Antes de llamar: tokens estimados (chars / 2.6) × `pricing.PRICES` → "repo X: ~N tokens ≈ $Y";
  confirmación en TTY o `--yes` en scripts. `KNOWLEDGE_INIT_MAX_USD_PER_REPO` (default 0.30)
  aborta ese repo si la estimación lo supera.
- Las citas que devuelve el modelo se verifican igual que las deterministas; las que no
  existen se descartan y la frase queda `[verificar]`.
- Métrica con `source=knowledge_init` para que `minka stats --source knowledge_init` lo muestre.

## 3. Seguridad

Nunca se lee ni se envía: `.env*`, `*.pem`, `*.key`, `*.p12`, `*.jks`, `id_rsa*`,
`credentials`, `secrets*`, `.aws/`, `vendor/`, `node_modules/`, `.venv/`, `dist/`, binarios
(> 1 MB o no UTF-8).

Escaneo del payload completo antes de la llamada (mismo formato de regla que `risk_analyzer`,
sumable con `RISK_PATTERNS` del proyecto):
`AKIA[0-9A-Z]{16}` · `BEGIN (RSA |EC )?PRIVATE KEY` · `eyJ[A-Za-z0-9_-]{20,}` (JWT) ·
`sk-[A-Za-z0-9]{20,}` · segmentos hex/base64 ≥ 40 chars · `password|secret|token\s*[:=]\s*["'][^"']{8,}`.
Un solo hit aborta con `ruta:línea (patrón)`; nunca se muestra el valor.

## 4. Escritura

- `minka knowledge init --project <p>` escribe solo el borrador en
  `output/knowledge_draft/<p>/` (`architecture.md`, `_sources.json`, `report.txt` con conteos) y
  muestra `difflib.unified_diff` contra `knowledge/extracted/`.
- `--apply` pide confirmación (vía `ApprovalGate.request`: diff + recibo) y copia: si el
  destino no existe lo crea; si existe, escribe `architecture.md.new` / `_sources.json.new` y lo
  dice. Nunca sobrescribe.
- Cada bullet termina en una cita verificada o en `[verificar]`. Al final de `--apply` corre
  `minka knowledge check` sobre el proyecto y reporta.

## 5. Contra el problema de R-01 (citas válidas, afirmaciones viejas)

- Encabezado obligatorio: `Generado: <fecha> · minka <versión> · repos: pos@<commit> hsm@<commit> …`
  y, por sección, `fuente: <repo>@<commit>`. Lo mismo en `_sources.json`.
- `knowledge check` gana el aviso `fuente_desactualizada` cuando `git_head(repo)` difiere del
  commit del manifiesto (freshness ya lo detecta; hoy no está en el CLI). Una cita puede
  seguir existiendo y la afirmación estar vieja: el commit lo delata.
- El borrador no escribe estados ni conclusiones de riesgo: eso queda en risk_catalog, con
  fecha y evidencia propias.

## 6. Pruebas de aceptación (sin red)

Repos temporales: python (pyproject + 2 paquetes con imports cruzados), go (go.mod + 2
paquetes), node (package.json + require relativo). Modelo simulado parcheando
`call_structured_result` para devolver un resumen con una cita válida y otra inventada.

Casos: árbol y conteos por extensión · imports internos correctos por stack · todas las citas
del borrador pasan `RepoIndex` · cita inventada del modelo → `[verificar]` · `.env` y `*.pem`
no aparecen en el payload · payload con `AKIA…` aborta listando la ruta sin el valor ·
estimación de costo y tope por repo · `--apply` sin destino crea, con destino escribe `.new` ·
encabezado con commit · `knowledge check` sobre el resultado = 0 rotas.

## 7. Tamaño y orden

1. **MVP** (≈400 líneas + 250 de tests): extractor determinista para python y go,
   `_sources.json`, borrador, draft + diff, `--apply` con `.new`, encabezado con commits.
2. node (imports relativos; `tsconfig paths` fuera del MVP).
3. Escaneo de secretos, obligatorio antes de habilitar `--with-model`.
4. `--with-model` con estimación, confirmación y tope por repo.
5. Aviso `fuente_desactualizada` en `knowledge check`.

Costo esperado: determinista $0 y < 2 s por repo (los 4 repos del proyecto de referencia:
899 archivos indexados en 18 ms); con modelo ~15k tokens de entrada por repo en Haiku
≈ $0.02–0.05, ≈ $0.15 el proyecto completo.

Riesgos: los imports internos en Go requieren el módulo del `go.mod` (si falta → `[verificar]`);
en node los alias de `tsconfig` esconden imports (MVP solo relativos); el prompt del modelo se
limita a "qué hace cada módulo" con citas, sin estados ni riesgos.
