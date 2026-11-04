"""
risk_analyzer — detección heurística de riesgos de seguridad y PCI-DSS.

Escanea el código Go buscando anti-patrones conocidos. Cada hallazgo incluye
archivo:línea para que el agente lo verifique. NO afirma culpa: señala para
revisión humana.
"""

import re
from pathlib import Path

from minka.config_loader import config

_SKIP = {"vendor", "node_modules", ".git"}

# Reglas: (id, severidad, regex, descripción)
_RULES = [
    (
        "PANIC_PROD",
        "ALTA",
        r"\bpanic\(",
        "panic() en código productivo (prohibido salvo mocks/tests)",
    ),
    (
        "HARDCODED_TOKEN",
        "CRÍTICA",
        r"0123456789ABCDEF",
        "Posible token/llave de ejemplo hardcodeado",
    ),
    (
        "PRIVATE_KEY",
        "CRÍTICA",
        r"BEGIN (RSA )?PRIVATE KEY",
        "Llave privada embebida en el repo (PCI-DSS)",
    ),
    (
        "SILENCED_ERR",
        "MEDIA",
        r"_\s*[,=]\s*\w+\([^)]*\)\s*//\s*ignore",
        "Error silenciado explícitamente",
    ),
    ("HTTP_INSECURE", "MEDIA", r"InsecureSkipVerify:\s*true", "TLS verification deshabilitada"),
    (
        "FMT_PRINT_PAN",
        "ALTA",
        r"(fmt\.Print|log\.Print).*(pan|Pan|PAN|cardNumber)",
        "Posible log de PAN sin enmascarar",
    ),
]


# Reglas por stack (core/stacks.py). Go conserva _RULES tal cual; las demás son genéricas.
_RULES_BY_STACK: dict[str, list[tuple]] = {
    "go": _RULES,
    "python": [
        (
            "EVAL_EXEC",
            "ALTA",
            r"\b(eval|exec)\s*\(",
            "eval()/exec() sobre datos: ejecución de código arbitrario",
        ),
        ("BARE_EXCEPT", "MEDIA", r"^\s*except\s*:", "except desnudo: traga cualquier error"),
        (
            "SHELL_TRUE",
            "ALTA",
            r"subprocess\.\w+\([^)]*shell\s*=\s*True",
            "subprocess con shell=True: inyección de comandos",
        ),
        ("PRIVATE_KEY", "CRÍTICA", r"BEGIN (RSA )?PRIVATE KEY", "Llave privada embebida en el repo"),
    ],
    "node": [
        ("EVAL", "ALTA", r"\beval\s*\(", "eval() sobre datos: ejecución de código arbitrario"),
        ("NEW_FUNCTION", "ALTA", r"new\s+Function\s*\(", "new Function(): eval encubierto"),
        (
            "CHILD_PROCESS_EXEC",
            "ALTA",
            r"child_process\)?\.exec\s*\(|\bexec\s*\(\s*`",
            "exec() de child_process con string: inyección de comandos",
        ),
        ("PRIVATE_KEY", "CRÍTICA", r"BEGIN (RSA )?PRIVATE KEY", "Llave privada embebida en el repo"),
    ],
}


def _rules() -> list[tuple]:
    """Reglas del STACK activo + config.RISK_PATTERNS: un id existente se reemplaza en su posición."""
    from minka.core import stacks

    rules = list(_RULES_BY_STACK.get(stacks.stack_name(), _RULES))
    for extra in getattr(config, "RISK_PATTERNS", []):
        idx = next((i for i, r in enumerate(rules) if r[0] == extra[0]), None)
        if idx is None:
            rules.append(extra)
        else:
            rules[idx] = extra
    return rules


def _go_files(root: Path):
    """Archivos de código del STACK activo (nombre histórico), sin vendor/mocks/node_modules."""
    from minka.core import stacks

    yield from stacks.code_files(root, extra_skip=("mocks",))


def detect_risks(path: str) -> str:
    """
    Ejecuta todas las reglas de riesgo sobre el repo y devuelve los hallazgos
    con severidad, id, archivo:línea y descripción.
    """
    root = Path(path)
    if not root.is_dir():
        return f"ERROR: no existe {path}"

    compiled = [(rid, sev, re.compile(rx), desc) for rid, sev, rx, desc in _rules()]
    findings: list[str] = []
    for f in _go_files(root):
        for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for rid, sev, rx, desc in compiled:
                if rx.search(line):
                    findings.append(f"[{sev}] {rid} — {f}:{n} — {desc}")

    if not findings:
        return "OK: no se detectaron riesgos con las reglas actuales."
    # Ordena por severidad
    order = {"CRÍTICA": 0, "ALTA": 1, "MEDIA": 2}
    findings.sort(key=lambda x: order.get(x.split("]")[0][1:], 9))
    return "\n".join(findings)


def check_pci_compliance(path: str) -> str:
    """
    Chequeo PCI-DSS enfocado: llaves, PAN en logs, tokens por defecto.
    Es un subconjunto de detect_risks orientado a compliance.
    """
    all_findings = detect_risks(path)
    pci_ids = ("HARDCODED_TOKEN", "PRIVATE_KEY", "FMT_PRINT_PAN", "HTTP_INSECURE")
    lines = [ln for ln in all_findings.splitlines() if any(pid in ln for pid in pci_ids)]
    return "\n".join(lines) if lines else "OK: sin hallazgos PCI-DSS con las reglas actuales."
