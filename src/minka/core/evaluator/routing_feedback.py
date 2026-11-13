"""routing_feedback — `/mal [agente_esperado] [nota]` en el REPL: el dev marca una decisión
del router como equivocada y queda una línea en projects/<p>/evals/routing_feedback.jsonl.

Cada línea lleva números y nombres de la corrida (run_id, router, reason, confidence, plan) más
el pedido normalizado (router._normalize + security.redact, hasta REQUEST_MAX_CHARS; si es más
largo, solo su sha256) y lo que dijo el dev (agente esperado, nota hasta NOTE_MAX_CHARS). NUNCA
respuestas, contenido de tools, historial ni reasoning. El archivo se crea 0600 y su carpeta
0700 porque contiene texto de los pedidos: no debe versionarse.

`minka evals routing --from-feedback` convierte ese archivo en evals/routing_cases.draft.json
para revisarlo a mano antes de promover casos a evals/routing_cases.json del proyecto (que
`routing_evals.info_cases` lee además del JSON del paquete).
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from minka.core import security
from minka.core.orchestrator import router

FEEDBACK_NAME = "routing_feedback.jsonl"
DRAFT_NAME = "routing_cases.draft.json"
PROJECT_CASES_NAME = "routing_cases.json"
REQUEST_MAX_CHARS = 120
NOTE_MAX_CHARS = 80
AGENTS = ("architecture", "development", "debug", "pr_review", "report", "meta")
USAGE = "Uso: /mal [agente_esperado] [nota hasta 80 caracteres] — marca la última decisión del router como equivocada."


def evals_dir(cfg) -> Path | None:
    project = getattr(cfg, "ACTIVE_PROJECT", None)
    if not project:
        return None
    return Path(cfg.PROJECTS_DIR) / project / "evals"


def feedback_path(cfg) -> Path | None:
    d = evals_dir(cfg)
    return d / FEEDBACK_NAME if d else None


def draft_path(cfg) -> Path | None:
    d = evals_dir(cfg)
    return d / DRAFT_NAME if d else None


def project_cases_path(cfg) -> Path | None:
    d = evals_dir(cfg)
    return d / PROJECT_CASES_NAME if d else None


def normalize_agent(name: str | None) -> str | None:
    """"architecture" / "architecture_agent" → "architecture_agent"; None si no es un agente."""
    if not name:
        return None
    short = name.strip().lower().removesuffix("_agent")
    return f"{short}_agent" if short in AGENTS else None


def parse_command(rest: str) -> tuple[str | None, str]:
    """Texto tras `/mal` → (agente esperado o None, nota recortada)."""
    parts = rest.strip().split(maxsplit=1)
    if not parts:
        return None, ""
    expected = normalize_agent(parts[0])
    note = (parts[1] if len(parts) > 1 else "") if expected else rest.strip()
    return expected, note[:NOTE_MAX_CHARS]


def request_fields(request: str) -> dict:
    """Pedido normalizado y redactado, o solo su hash si es largo."""
    norm = security.redact(router._normalize(request or ""))[0]
    if len(norm) <= REQUEST_MAX_CHARS:
        return {"request_norm": norm, "request_sha": None}
    return {"request_norm": None, "request_sha": hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]}


def build_entry(record: dict, request: str, expected: str | None, note: str, project: str | None) -> dict:
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": record.get("run_id"),
        "project": project,
        "router": record.get("router"),
        "reason": record.get("reason"),
        "confidence": record.get("confidence"),
        "plan": list(record.get("plan") or []),
        **request_fields(request),
        "expected": normalize_agent(expected),
        "note": (note or "")[:NOTE_MAX_CHARS],
    }


def _secure_append(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(path, 0o600)


def write_feedback(cfg, record: dict, request: str, expected: str | None, note: str) -> Path:
    """Agrega una línea al jsonl del proyecto activo (0600, carpeta 0700). Devuelve la ruta."""
    path = feedback_path(cfg)
    if path is None:
        raise ValueError("no hay proyecto activo")
    entry = build_entry(record, request, expected, note, getattr(cfg, "ACTIVE_PROJECT", None))
    _secure_append(path, json.dumps(entry, ensure_ascii=False))
    return path


def read_feedback(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def draft_cases(cfg) -> tuple[Path, int, int]:
    """feedback → borrador de casos golden (eval=info) para revisar a mano.
    Devuelve (ruta, casos escritos, entradas omitidas por no tener texto)."""
    src, dst = feedback_path(cfg), draft_path(cfg)
    if src is None or dst is None:
        raise ValueError("no hay proyecto activo")
    cases, skipped = [], 0
    for i, fb in enumerate(read_feedback(src), 1):
        if not fb.get("request_norm"):
            skipped += 1  # solo hash: no hay pedido que reproducir
            continue
        expected = normalize_agent(fb.get("expected"))
        cases.append(
            {
                "id": f"feedback_{i}",
                "_nota": "BORRADOR desde /mal: revisar a mano. " + (fb.get("note") or ""),
                "request": fb["request_norm"],
                "eval": "info",
                "expect": {"agents": [expected] if expected else [], "router": None, "llm_called": None},
                "observed": {"router": fb.get("router"), "plan": fb.get("plan"), "run_id": fb.get("run_id")},
            }
        )
    payload = {
        "_doc": "Borrador generado por `minka evals routing --from-feedback`. Revisar, completar expect "
        "y mover a evals/routing_cases.json del proyecto. Contiene texto de pedidos: no versionar.",
        "cases": cases,
    }
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(dst, 0o600)
    return dst, len(cases), skipped
