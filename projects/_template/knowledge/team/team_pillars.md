# Pilares del equipo — Skill del Agente
# PLANTILLA: reemplaza este contenido con los valores oficiales de TU equipo.

> Cómo completarlo:
> 1. Copia en "Textos oficiales" los pilares del equipo tal cual (sin resumir).
> 2. Traduce cada pilar a reglas técnicas verificables: señales de violación
>    concretas y la pregunta de control que hará el agente.
> 3. Refleja los mismos pilares en el `config.py` del proyecto:
>    `TEAM_PILLARS` (una línea por pilar, la que muestra el ApprovalGate),
>    `TEAM_PILLARS_HEADING`, `TEAM_PILLARS_READABILITY` y
>    `TEAM_PILLARS_LINE_CAP_HINT`. Vacíos = el gate y las lentes los omiten.
> 4. Este doc lo cargan development_agent y pr_review_agent vía
>    `AGENT_KNOWLEDGE` (`team/team_pillars.md`).

---

## Textos oficiales del equipo (NO modificar ni resumir)

**01 <Nombre del pilar>**
"<texto oficial del pilar>"

**02 <Nombre del pilar>**
"<texto oficial del pilar>"

---

## Traducción a reglas técnicas

### Regla 1 — <Pilar>
- <regla verificable en el código>
- Señal de violación: <ejemplo concreto>
- Pregunta del agente: "<pregunta de control>"

---

## EJEMPLO (ilustrativo, bórralo al completar)

### Regla — Simplicidad
- Rechaza abstracciones sin caso de uso real hoy.
- Señal de violación: función >50 líneas sin justificación.
- Pregunta del agente: "¿Esta complejidad resuelve un problema del ticket actual?"

### Regla — Logs con contexto de negocio
  Ejemplo correcto:
    logger.Info("transacción procesada",
      zap.String("processor", "<procesador>"),
      zap.String("transaction_id", req.TransactionID),
    )
  Ejemplo incorrecto:
    log.Printf("done")

---

## Cómo el agente aplica estos pilares

### En development_agent
Antes de proponer cualquier código, evalúa el cambio contra cada regla.
El score aparece antes del diff en el ApprovalGate.

### En pr_review_agent
Al final de cada revisión incluye la tabla de auditoría cultural
(una fila por pilar):

| Pilar | Estado | Evidencia |
|-------|--------|-----------|
| <Pilar> | ✓ CUMPLE / ⚠ REVISAR | razón con archivo:línea |

Un ⚠ REVISAR NO es bloqueante por sí solo, pero debe explicarse.
