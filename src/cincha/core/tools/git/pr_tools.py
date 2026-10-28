"""
pr_tools — generación de descripciones de PR y validación de diffs.

No abre PRs contra Bitbucket automáticamente (regla: los PRs/commits los
gestiona el dev). Genera el material que el dev pega en el PR.
"""

from cincha.config_loader import config
from cincha.core.tools.git.git_tools import show_diff

# Alertas de validate_diff: (needle en minúsculas, mensaje).
_DIFF_ALERTS = [
    ("panic(", "⚠️ Se agregó panic() — prohibido en código productivo."),
    ("secret", "⚠️ Aparece 'secret' en el diff — revisa que no sea un valor hardcodeado."),
    ("0123456789abcdef", "⚠️ Token/llave de ejemplo hardcodeado detectado."),
    ("todo", "ℹ️ Se agregó un TODO — registrar en tech_debt.md."),
    ("private_key", "⚠️ Posible llave privada en el diff (PCI-DSS)."),
]


def _diff_alerts() -> list[tuple]:
    """_DIFF_ALERTS + config.PR_DIFF_ALERTS: un needle existente se reemplaza en su posición."""
    alerts = list(_DIFF_ALERTS)
    for extra in getattr(config, "PR_DIFF_ALERTS", []):
        idx = next((i for i, a in enumerate(alerts) if a[0] == extra[0]), None)
        if idx is None:
            alerts.append(extra)
        else:
            alerts[idx] = extra
    return alerts


def create_pr_description(repo_path: str, title: str, summary: str) -> str:
    """
    Genera una plantilla de descripción de PR en Markdown, incluyendo el diff
    actual del repo para revisión rápida. El checklist viene de
    config.PR_CHECKLIST (parametrizable por proyecto).
    """
    diff = show_diff(repo_path)
    checklist = (
        "\n".join(f"- [ ] {item}" for item in getattr(config, "PR_CHECKLIST", []))
        or "- [ ] Revisión general del cambio"
    )
    return (
        f"## {title}\n\n"
        f"### Resumen\n{summary}\n\n"
        f"### Checklist\n{checklist}\n\n"
        f"### Diff\n```diff\n{diff}\n```\n"
    )


def validate_diff(repo_path: str) -> str:
    """
    Validación heurística del diff actual: alerta sobre patrones peligrosos
    (secretos, panic, TODO nuevos). No sustituye la revisión humana.
    """
    diff = show_diff(repo_path)
    if diff == "[sin cambios]":
        return "[sin cambios que validar]"

    alerts: list[str] = []
    lowered = diff.lower()
    for needle, msg in _diff_alerts():
        if needle in lowered:
            alerts.append(msg)

    return "\n".join(alerts) if alerts else "OK: sin patrones peligrosos detectados en el diff."
