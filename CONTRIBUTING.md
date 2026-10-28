# Contribuir al dev-agent

## Las 3 capas — qué puedes modificar

**Capa 3 — Tu proyecto (modifica libremente)**
`projects/[tu-proyecto]/` → solo afecta tu proyecto

**Capa 2 — Motor genérico (con cuidado)**
`core/` → afecta todos los proyectos
Lee ADVERTENCIAS_CORE.md antes de modificar.

**Capa 1 — Estándares globales (consenso del equipo)**
`projects/[proyecto]/knowledge/team/team_pillars.md`
No modificar sin discusión del equipo.

## Para agregar un proyecto nuevo
Sigue los 6 pasos de `projects/_template/README.md`.

## Para mejorar el motor (core/)
1. Documenta el problema en `<DATA_DIR>/active-context/workspace.md`
2. Propón el cambio en sesión dedicada con Claude Code
3. Un cambio a la vez con verificación entre cada uno
4. Documenta la decisión en `CLAUDE.md` y `decisions_log.md`

## Convenciones
- Diffs antes de modificar archivos existentes
- Los commits los hace el dev, nunca el agente
- Todo cambio en core/ se verifica con `minka --help`