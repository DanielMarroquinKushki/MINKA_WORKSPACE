# Cambios

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
