# ⚠️ Advertencias — Modificar dev-agent/core/

Leer antes de tocar cualquier archivo en `core/`.
Los cambios aquí afectan TODOS tus proyectos en el workspace.

---

## Arquitectura de 3 capas

```
Capa 1 — Estándares globales (nunca modificar sin consenso)
  knowledge/team/team_pillars.md
  → Cambiarlo afecta los valores que auditan TODOS los agentes

Capa 2 — Motor genérico (modificar con cuidado)
  core/agents/        → afecta el comportamiento de todos los agentes
  core/orchestrator/  → afecta el flujo de todas las tareas
  core/tools/         → afecta las capacidades de todos los agentes
  core/memory/        → afecta la memoria de todas las sesiones

Capa 3 — Tu proyecto (modifica libremente)
  projects/[nombre]/  → solo afecta tu proyecto
```

---

## Antes de modificar core/ — checklist obligatorio

- [ ] ¿El cambio es realmente necesario o puedo resolverlo en projects/?
- [ ] ¿Entiendo qué otros proyectos usa este componente?
- [ ] ¿Tengo una forma de verificar que no rompí nada? (minka --help)
- [ ] ¿Documenté el cambio en CLAUDE.md y decisions_log.md?

---

## Archivos de alto riesgo

| Archivo | Riesgo | Qué pasa si falla |
|---------|--------|-------------------|
| `core/orchestrator/orchestrator.py` | 🔴 MUY ALTO | Ningún agente puede ejecutar tareas |
| `core/tools/registry.py` | 🔴 MUY ALTO | Los agentes pierden todas sus tools |
| `core/agents/base_agent.py` | 🔴 MUY ALTO | Ningún agente arranca |
| `core/orchestrator/approval_gate.py` | 🟠 ALTO | Las escrituras no piden aprobación |
| `core/orchestrator/state_manager.py` | 🟠 ALTO | Se pierde el contexto entre agentes |
| `core/memory/session_memory.py` | 🟡 MEDIO | Se pierde el historial de sesión |
| `core/knowledge/loader.py` | 🟡 MEDIO | Los agentes no cargan su knowledge |

---

## Verificación obligatoria después de cualquier cambio en core/

```bash
# 1. Verifica que el agente arranca
minka --help

# 2. Verifica que el proyecto carga
minka --project <proyecto>
> /status

# 3. Prueba una tarea simple de solo lectura
> analiza la estructura del proyecto
```

Si alguno falla → revierte el cambio antes de continuar.

---

## Cómo proponer mejoras al core correctamente

Si encontraste algo que mejorar en el core:

1. Documenta el problema en `<DATA_DIR>/active-context/workspace.md`
2. Propón el cambio en una sesión dedicada con Claude Code
3. Aplica un cambio a la vez con verificación entre cada uno
4. Documenta la decisión en `dev-agent/CLAUDE.md`

Las mejoras al core benefician a todos — pero los errores también.
