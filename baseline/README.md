# Línea base del motor

`prompt_hash_motor.txt` es la huella (`core/baseline.compute_prompt_hash`: prompts + schemas de
tools por agente + MODELS) del motor **sin proyecto activo** (MINKA_HOME vacío). El job
`prompt-hash` del CI la compara y falla si cambió.

Cambiarla a propósito (prompt, tool o modelo nuevo):

```bash
MINKA_HOME=$(mktemp -d) uv run --isolated python -c \
  "from minka.core.baseline import compute_prompt_hash; print(compute_prompt_hash())" \
  > baseline/prompt_hash_motor.txt
```

y mencionar en el commit por qué se movió. Si se movió sin querer, el cambio hay que revertirlo.
