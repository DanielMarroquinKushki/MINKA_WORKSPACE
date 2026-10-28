# Plantilla de proyecto — kushki-workspace

Guía para crear un proyecto nuevo en 6 pasos.
Al terminar tendrás el agente funcionando con tus repos.

> Los prompts genéricos de esta plantilla funcionan desde el día 1.
> Especialízalos gradualmente según las necesidades de tu proyecto.

---

## Camino rápido (recomendado)

```bash
./agent --new-project [mi-proyecto]     # o: kw new [mi-proyecto]
```

Hace automáticamente los pasos 1-3: copia la plantilla, renombra los `.example`
(config, session_log, decisions_log) y crea `active-context/[mi-proyecto].md`.
Te pregunta el `CORE_MODULE_NAME`.

Solo te quedan los pasos específicos de tu máquina:
- **Paso 2** — completar el resto de `config.py` (PR_CHECKLIST, AGENT_KNOWLEDGE, AGENT_TOOLS).
- **Paso 5** — crear el knowledge base del proyecto.
- **Paso 6** — symlinks de repos + `REPOS` en `config_local.py`.

Los pasos de abajo quedan como referencia detallada.

---

## Paso 1 — Copia la plantilla

```bash
cp -r dev-agent/projects/_template dev-agent/projects/[mi-proyecto]
```

## Paso 2 — Configura el proyecto

Renombra y completa `config.py.example → config.py`:
- `CORE_MODULE_NAME` — nombre del módulo compartido entre repos
- `PR_CHECKLIST` — checklist específico de tu proyecto
- `AGENT_KNOWLEDGE` — qué archivos de knowledge carga cada agente
- `AGENT_TOOLS` — qué tools tiene cada agente

## Paso 3 — Crea la memoria del proyecto (3 capas)

El proyecto usa 3 capas de memoria. Renombra las plantillas y complétalas
(Claude Code las gestiona automáticamente después — ver "Sistema de memoria"
en el `CLAUDE.md` del workspace):

1. **Contexto activo (corto plazo, máx 30 líneas)** — crea
   `active-context/[mi-proyecto].md` en la raíz del workspace (fuente única):
   ticket actual, alcance, bloqueos, próximo paso y stack.
2. **Historial de sesiones** — `knowledge/team/session_log.md.example → session_log.md`:
   bitácora cronológica de qué se hace cada día.
3. **Decisiones (ADRs)** — `knowledge/team/decisions_log.md.example → decisions_log.md`:
   contexto → decisión → consecuencias.

> A partir de aquí el dev no gestiona qué archivo contiene qué: Claude Code mueve
> el exceso del contexto a `session_log.md`, la arquitectura a
> `knowledge/extracted/architecture.md` y las decisiones a `decisions_log.md`,
> mostrando siempre el diff para aprobación.

## Paso 4 — Especializa los prompts (opcional pero recomendado)

Los prompts en `prompts/` (7 archivos: 5 agentes + orchestrator + meta)
funcionan tal cual para cualquier proyecto. Cada uno indica en su
docstring qué personalizar. Para especializarlos agrega al final de
cada uno las reglas específicas de tu dominio. Ejemplo:

```python
# Al final de development_prompt.py de tu proyecto:
DEVELOPMENT_SYSTEM_PROMPT += """

## Reglas específicas de [mi-proyecto]
- Usa siempre el logger de [mi-librería] con campos semánticos
- Interfaces máximo 5 métodos (convención del equipo)
- [tus reglas específicas aquí]
"""
```

⚠️ No quites las secciones estándar del workspace (Capa 1):
"Lo que NUNCA hago", "Manejo de errores", "Pilares del equipo" y la
"Auditoría cultural" — aplican a todos los proyectos.

## Paso 5 — Crea el knowledge base

Crea los archivos de conocimiento específico de tu proyecto:
```
knowledge/
├── extracted/    ← ejecuta el agente de arquitectura para generarlo
├── professional/ ← conocimiento técnico de tu dominio
└── team/         ← convenciones y decisiones de tu equipo
```

Para generar `extracted/` automáticamente:
```
kw [mi-proyecto]
> analiza la arquitectura completa del proyecto y
  genera el knowledge base en knowledge/extracted/
```

## Paso 6 — Agrega los symlinks de tus repos

```bash
mkdir -p kushki-workspace/repos/[mi-proyecto]
cd kushki-workspace/repos/[mi-proyecto]
ln -s [ruta-real]/[repo] [repo]
```

Agrega las rutas en tu `config_local.py`:
```python
REPOS = {
    "repo1": str(Path.home() / "..."),
}
```

Arranca: `./agent --project [mi-proyecto]` desde `dev-agent/`.

## Paso extra (opcional) — Arranque directo desde los repos

Copia `CLAUDE.md.example` como `repos/[mi-proyecto]/CLAUDE.md` —
detecta el proyecto solo, no requiere edición. Exclúyelo de git
vía `.git/info/exclude`.

---

## Notas

- `REPOS` va en `config_local.py` (rutas de TU máquina), no en el
  config del proyecto.
- Los `.md` genéricos del motor (go_best_practices,
  hexagonal_architecture, design_patterns, code_review_checklist) se
  cargan automáticamente por fallback — no los copies al proyecto.

---

## Lo que puedes personalizar libremente (Capa 3)

✓ Prompts de agentes — especialízalos para tu dominio
✓ Knowledge base — agrega todo el contexto que necesites
✓ Config del proyecto — tus repos, tools y knowledge
✓ active-context/[proyecto].md — tu memoria de sesión (raíz del workspace)

## Lo que debes respetar siempre (Capa 1)

Los pilares del equipo (`TEAM_PILLARS` en el `config.py` del proyecto) aplican
a todos los proyectos. Ejemplo:
- Urgencia: solución más simple que resuelve el problema
- Curiosidad: justifica decisiones con datos
- No ego: código legible para cualquier seniority
- Misión: conecta el código con el impacto en el negocio

También son Capa 1 las secciones estándar de los prompts:
"Lo que NUNCA hago" y "Manejo de errores" (Stop→Report→Propose→Ask).

## Lo que modificas con cuidado (Capa 2)

Si necesitas cambiar algo en `dev-agent/core/`, lee primero
`dev-agent/ADVERTENCIAS_CORE.md` — afecta todos tus proyectos.
