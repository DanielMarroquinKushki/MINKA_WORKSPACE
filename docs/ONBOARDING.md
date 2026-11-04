# Onboarding — minka (dev-agent)

Dos recorridos: **usuario nuevo** (6 pasos, de cero a la primera consulta) y
**usuario actual que agrega un proyecto**. Todo lo que es tuyo (rutas de repos, perfil
AWS, API key, contexto vivo) vive en el directorio de datos, nunca en el repo del motor.

El directorio de datos es `MINKA_HOME` si está definido, o `~/.minka/` si no. Si tu
equipo guarda `projects/` y `active-context/` en un workspace compartido, exporta
`MINKA_HOME` apuntando a su carpeta `dev-agent/` antes de empezar.

---

## Usuario nuevo en 6 pasos

1. **Instala** (requiere Python ≥ 3.12 y uv):
   ```bash
   uv tool install git+https://github.com/DanielMarroquinKushki/MINKA_WORKSPACE
   # con búsqueda semántica (RAG):
   uv tool install 'minka-workspace[rag] @ git+https://github.com/DanielMarroquinKushki/MINKA_WORKSPACE'
   ```
2. **Crea tu configuración** con el asistente. Pregunta los alias y rutas de tus repos y
   el backend (Bedrock por SSO o API directa); solo escribe en archivos nuevos y nunca
   muestra lo que tecleas:
   ```bash
   minka init
   ```
   Si ya tenías `config_local.py` o `.env`, no los toca: edítalos a mano.
3. **Autentícate.** Bedrock: `minka-sso` (crea o verifica el perfil, abre el browser).
   API directa: pon `ANTHROPIC_API_KEY` en `<DATA_DIR>/.env` y `LLM_BACKEND=anthropic`.
4. **Diagnostica** antes de la primera corrida:
   ```bash
   minka doctor
   ```
   Cada línea sale con ✓ o ✗ y qué hacer. Los ✗ bloqueantes (Python, sesión, REPOS,
   proyecto) dan exit 1; chromadb y el contexto vivo solo avisan.
5. **Crea tu primer proyecto** (nombre en kebab-case):
   ```bash
   minka --new-project mi-proyecto
   ```
   Genera `projects/mi-proyecto/` (config, prompts, knowledge) y el contexto vivo
   `active-context/mi-proyecto.md` en `ACTIVE_CONTEXT_DIR`. Completa `config.py` del
   proyecto y pon `DEFAULT_PROJECT = "mi-proyecto"` en `config_local.py`.
6. **Primera consulta:**
   ```bash
   minka "¿dónde se resuelve el token en el repo <alias>?"
   minka                       # modo interactivo; /status, /clear, salir
   ```
   Las consultas puntuales salen breves con citas `archivo:línea`; pide "en detalle" o
   usa `--completo` para la versión larga. `--plain` (o `MINKA_PLAIN=1`) imprime sin
   Markdown. `minka stats` muestra tokens, costo y la línea base (`prompt_hash`).

---

## Usuario actual: agregar un proyecto

1. `minka --new-project nuevo-proyecto` (o `minka --new-project` y responde el nombre).
2. En `<DATA_DIR>/config_local.py` agrega los repos del proyecto a `REPOS` con sus alias.
   Ejemplo genérico con un repo de utilidades compartidas:
   ```python
   REPOS = {
       "pos":  "/ruta/a/svc-pos",
       "core": "/ruta/a/svc-core",
       "payment-crypto": "/ruta/a/svc-payment-crypto",
   }
   ```
3. Completa `projects/nuevo-proyecto/config.py` (CORE_MODULE_NAME, PR_CHECKLIST,
   AGENT_KNOWLEDGE, AGENT_TOOLS) y deja el knowledge en `knowledge/extracted/`.
4. Si el contexto vivo del equipo vive fuera del directorio de datos, apunta
   `ACTIVE_CONTEXT_DIR` en `config_local.py` a esa carpeta.
5. Verifica: `minka doctor --project nuevo-proyecto` y, cuando haya knowledge,
   `minka knowledge check --project nuevo-proyecto` (citas rotas → exit 1).
6. Trabaja con `minka --project nuevo-proyecto` o fija `DEFAULT_PROJECT`.

---

## Si algo falla

| Síntoma | Qué hacer |
|---|---|
| "Sesión AWS expirada" | `aws sso login --profile <perfil>` en otra terminal; el agente retoma |
| `minka doctor` marca REPOS en ✗ | corrige las rutas en `config_local.py` (deben existir en tu disco) |
| El agente no encuentra el contexto vivo | revisa `ACTIVE_CONTEXT_DIR` y que exista `<proyecto>.md` |
| Respuesta con `⚠ no existe` junto a una cita | el archivo citado no está en REPOS: la cita es falsa o el repo cambió |
| Salida con formato raro | `minka --plain` o `MINKA_PLAIN=1` |
