# Paso 6b — Verificar las citas `archivo:línea` de la respuesta del agente

Estado: diseño aprobado, pendiente de implementar.
Decisión tomada: la memoria de la sesión guarda el texto **anotado** (marcador corto
`⚠ no existe`), para que el siguiente turno herede la cita ya marcada y el agente no
la repita como cierta.

## Problema

Los agentes citan código con `ruta/archivo.go:123`. Una parte de esas citas apunta a
archivos que ya no existen o a líneas fuera de rango (observado: ~3 de 18 en una
respuesta). Como el historial único (`ConversationMemory`) conserva las líneas con cita,
una cita falsa se hereda a los turnos siguientes con la misma seguridad que una real.

## 1. Dónde engancha

| Pieza | Hoy | Cambio |
|---|---|---|
| `orchestrator._record_turn` (orchestrator.py:104) | único camino hacia `ConversationMemory.add`; lo llaman el plan (`final`, :425) y `_run_forced` (:222) | si hay `result`, llama a `citation_check.verify(result, self.repo_index)`; guarda en memoria el texto anotado y deja `self.last_citations = (total, missing)` |
| `Orchestrator.__init__` | — | crea `self.repo_index = RepoIndex(config.REPOS)` una vez por sesión; se reconstruye si `set_project` cambia REPOS |
| `Orchestrator.run` | devuelve `result` | devuelve el texto anotado (mismo texto, marcadores añadidos; nunca se reescribe prosa) |
| `main._print_run_summary` (main.py:203) | líneas de decisión y tokens en stderr, respeta `--quiet` y `QUIET_ENV` | añade `N citas verificadas, M no encontradas` solo si N > 0 |
| `metrics.build_run_record` (metrics.py:46) | números por kwargs | nuevos `citations_total` y `citations_missing`; `aggregate` y `stats` los suman como `repeated_tool_calls` |
| `config.py` | `PROMPT_CACHE_ENABLED` por env o config_local | `CITATION_CHECK_ENABLED` con el mismo patrón; apagado = texto intacto y contadores en 0 |

Un solo punto de enganche cubre el agente forzado y el plan de varios pasos. Se anota
el texto consolidado completo (con cabeceras `### [agente]`); la memoria sigue quitando
las cabeceras y recortando con `trim_result`, que nunca parte una línea con cita, así
que el marcador viaja junto a la cita.

Módulo nuevo: `src/minka/core/orchestrator/citation_check.py`.

```
verify(text: str, index: RepoIndex) -> tuple[str, int, int]
    # (texto_anotado, citas_verificadas, no_encontradas)
```

## 2. Resolución

Se reutiliza `core/tools/analysis/citations.py` sin cambios:

- `RepoIndex.resolve`: ruta literal bajo cada repo → alias (primer segmento = clave de
  REPOS, sin distinguir mayúsculas) → nombre del directorio del repo → basename único.
  Rechaza `..` y symlinks que salgan de REPOS (`fuera_de_repos`).
- Cuentan como **no encontradas**: `inexistente`, `fuera_rango`, `fuera_de_repos`.
- **No se marcan**: `ok`, `ambigua` (varios homónimos), `elidida` (`.../`).
- Chequeo de línea con el archivo real (`fuera_rango` si la línea supera el largo).
- No se aplica el aviso de símbolo movido: en una respuesta no hay identificador en
  backticks asociado de forma fiable a cada cita.

### Falsos positivos

- Usar `citations.CITATION_RE` (extensiones de código, `CITED_EXTENSIONS`), no la de
  `conversation_memory` (acepta cualquier extensión).
- Saltar los bloques de código: recorrer por líneas con estado "dentro de fence"
  (``` o ~~~); lo que está dentro no se verifica.
- Enmascarar literales entre comillas simples o dobles antes de buscar citas en la línea
  (rutas de ejemplo como `"config/app.yml:1"` en prosa).
- Las citas en backticks **sí** se verifican: es el formato que usan los agentes.
- Una misma cita repetida se resuelve una vez (caché por texto) y se anota en cada
  aparición.

## 3. Salida al usuario

- Anotación inline, inmediatamente después de la cita, sin tocar el resto de la línea:
  - `ruta/archivo.go:12 ⚠ no existe`
  - `ruta/archivo.go:900 ⚠ línea fuera de rango`
- Si la cita va en backticks, el marcador queda fuera del backtick.
- Línea final en stderr, vía `_print_run_summary`:
  `N citas verificadas, M no encontradas`. Se omite con `--quiet` o `QUIET_ENV`, y
  cuando N = 0.
- Interruptor `CITATION_CHECK_ENABLED` (default `true`). Apagado: `run` devuelve el texto
  original, la memoria guarda el texto original, contadores en 0 y sin línea final.

## 4. Costo y latencia

- Cero tokens: solo filesystem, sin git ni modelo.
- Medido en los 4 repos del proyecto activo: 899 archivos indexados en 18 ms;
  4 resoluciones en 0,3 ms. El índice de basenames se construye una vez por sesión
  (`RepoIndex` vive en el `Orchestrator`), así el REPL no repite el `os.walk`.
- Si REPOS está vacío o sin rutas válidas (`RepoIndex.empty`), la verificación se
  salta y no anota nada.

## 5. Tests (`tests/test_citation_check.py`)

Repos temporales en `tmp_path` (uno por symlink), sin red, sin modelo, sin DATA_DIR real
(conftest ya lo aísla), nombres neutros (`pagos`, `ledger`, `usrv-demo`).

1. Anota `⚠ no existe` en cita a archivo borrado y `⚠ línea fuera de rango` en línea
   mayor que el largo; el resto de la línea queda idéntico.
2. No anota citas `ok`, `ambigua` ni `elidida`; alias de REPOS y basename único resuelven.
3. Ignora citas dentro de bloques ``` y dentro de literales entre comillas.
4. Texto sin citas → devuelve el mismo string y `(0, 0)`.
5. `_record_turn` guarda el texto anotado: `memory.as_messages()` del siguiente turno
   contiene el marcador; las cabeceras `### [agente]` siguen fuera de la memoria.
6. `Orchestrator.run` devuelve el texto anotado (agente simulado, sin LLM) y
   `last_citations` coincide con los contadores.
7. `build_run_record` lleva `citations_total` y `citations_missing`; `aggregate` los suma.
8. `_print_run_summary` imprime la línea con N > 0 y la omite con `--quiet` o con N = 0.
9. `CITATION_CHECK_ENABLED=False`: texto intacto, memoria sin marcador, contadores en 0.
10. Cita repetida: se cuenta cada aparición y se anota cada una.

## Fuera de alcance

- No reescribe ni corrige citas: solo marca.
- No propone la ubicación nueva (eso lo hace `minka knowledge check` + revisión humana).
- No toca el knowledge ni `active-context`.
