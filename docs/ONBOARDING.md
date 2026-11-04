# Onboarding — minka (dev-agent)

Dos recorridos: **usuario nuevo** (6 pasos, de cero a la primera consulta) y
**usuario actual que agrega un proyecto**. Todo lo que es tuyo (rutas de repos, perfil
AWS, API key, contexto vivo) vive en el directorio de datos, nunca en el repo del motor.

El directorio de datos es `MINKA_HOME` si está definido, o `~/.minka/` si no. Si tu
equipo guarda `projects/` y `active-context/` en un workspace compartido, exporta
`MINKA_HOME` apuntando a su carpeta `dev-agent/` antes de empezar.

---

## Usuario nuevo: 4 comandos

```bash
# 1. Instalar (Python ≥ 3.12 y uv). Con búsqueda semántica: 'minka-workspace[rag] @ git+…'
uv tool install git+https://github.com/DanielMarroquinKushki/MINKA_WORKSPACE

# 2. Configuración personal (directorio de datos, config_local.py, .env, backend). No sobrescribe.
minka init

# 3. Proyecto: detecta el stack del primer repo (go.mod / pyproject.toml / package.json),
#    escribe STACK, COMPLIANCE y REPOS en projects/<p>/config.py, corre el doctor y sale.
minka --new-project mi-proyecto --repo api=~/code/svc-api --repo core=~/code/svc-core
#    interactivo (pregunta repos, stack y si aplica PCI):  minka --new-project mi-proyecto
#    sin preguntas para scripts:  … --stack python --compliance pci --yes

# 4. Verificar y empezar (dentro de un repo del proyecto no hace falta --project:
#    minka infiere el proyecto por el directorio actual y lo anuncia "(por cwd)")
cd ~/code/svc-api
minka doctor
minka "¿dónde se resuelve el token en el repo api?"
```

Orden de resolución del proyecto activo: `--project` > directorio actual (el repo donde
estás, también por symlink) > `DEFAULT_PROJECT` de `config_local.py`. Si el repo pertenece a
dos proyectos y ninguno es el default, minka pregunta en terminal y falla con la lista en
scripts; el bridge exige siempre `--project`.

Notas: Bedrock requiere `minka-sso` una vez (perfil SSO); API directa, `ANTHROPIC_API_KEY`
en `<DATA_DIR>/.env` y `LLM_BACKEND=anthropic`. Si todos los repos comparten carpeta,
`--new-project` los deja relativos y fija `REPOS_ROOT` en `config_local.py`; si no, quedan
absolutos. Nunca sobrescribe un proyecto existente. Las consultas puntuales salen breves con
citas `archivo:línea` ("en detalle" o `--completo` para la versión larga); `--plain` imprime
sin Markdown; `minka stats` muestra tokens, costo y la línea base (`prompt_hash`).

## Cargar contexto (lo mínimo para que el agente vea tu código)

1. **REPOS** declarados en `projects/<p>/config.py` (los crea `--new-project`); `minka doctor`
   debe mostrar `Repos n/n`.
2. **Índice de código**: `minka --index-repos --project <p>` (requiere `RAG_ENABLED = True` y el
   extra `[rag]`). Incremental: solo reindexa lo que cambió; repítelo tras cambios grandes.
3. **Contexto vivo**: `active-context/<p>.md` (lo crea `--new-project`, ~20 líneas máximo):
   ticket actual, alcance, bloqueo y próximo paso. Viaja al agente en cada consulta y
   responde "¿en qué quedamos?" sin gastar tokens.
4. **Knowledge**: deja `knowledge/extracted/*.md` para lo que se derive del código real con
   citas `archivo:línea`; `minka knowledge check --project <p>` avisa cuando queden rotas.

**Qué NO poner** en active-context, knowledge ni prompts: secretos, llaves privadas, tokens,
API keys, PAN u otros datos de tarjeta, credenciales de SSO ni URLs internas con token. Todo
eso viaja al modelo; las rutas de repos y los nombres de archivo sí son necesarios.

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
