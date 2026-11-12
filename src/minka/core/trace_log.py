"""trace_log — traza local por corrida (`minka trace`), apagada por defecto.

Un JSONL por corrida en <DATA_DIR>/output/trace/<día>/<run_id>.jsonl (directorios 0700,
archivos 0600). Eventos: route, turn, tool, end. Nunca el texto del modelo ni el contenido
que devuelve una tool: solo nombres, números y argumentos ya filtrados, y todo texto libre
pasa por security.redact. Apagada (`TRACE_ENABLED` false y sin `--trace`): `open_run` devuelve
None y los agentes no hacen nada más que un `if`.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from minka.core import security

DEFAULT_KEEP_DAYS = 7
_DAY_FMT = "%Y-%m-%d"
EVENT_KINDS = ("route", "turn", "tool", "end")


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "TRACE_ENABLED", False))


def trace_dir(cfg) -> Path:
    return Path(getattr(cfg, "OUTPUT_DIR")) / "trace"


def _mkdir_private(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _clean(value):
    """Texto → redactado y acotado; dict/list → recursivo; números y bool tal cual."""
    if isinstance(value, str):
        return security.redact(value[:500])[0]
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return security.redact(str(value)[:500])[0]


class RunTrace:
    def __init__(self, path: Path, run_id: str):
        self.path = path
        self.run_id = run_id
        self.events = 0

    def event(self, kind: str, **fields) -> None:
        """Una línea JSON. Best-effort: un fallo de disco nunca rompe la corrida."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"evento de traza desconocido: {kind}")
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "event": kind, **_clean(fields)}
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self.events += 1
        except OSError:
            pass


def rotate(cfg, now: datetime | None = None) -> int:
    """Borra los directorios <día> con más de TRACE_KEEP_DAYS días. Devuelve cuántos retiró."""
    root = trace_dir(cfg)
    if not root.is_dir():
        return 0
    keep = int(getattr(cfg, "TRACE_KEEP_DAYS", DEFAULT_KEEP_DAYS) or DEFAULT_KEEP_DAYS)
    cutoff = ((now or datetime.now()).date() - timedelta(days=keep)).strftime(_DAY_FMT)
    removed = 0
    for day_dir in sorted(root.iterdir()):
        if not day_dir.is_dir():
            continue
        try:
            datetime.strptime(day_dir.name, _DAY_FMT)
        except ValueError:
            continue
        if day_dir.name < cutoff:
            shutil.rmtree(day_dir, ignore_errors=True)
            removed += 1
    return removed


def open_run(cfg, run_id: str, announce=None) -> RunTrace | None:
    """Abre la traza de una corrida si está activa; si no, None (sin tocar disco).
    `announce(line)` recibe la línea de rotación cuando se retiró algo."""
    if not enabled(cfg):
        return None
    removed = rotate(cfg)
    if removed and announce is not None:
        keep = int(getattr(cfg, "TRACE_KEEP_DAYS", DEFAULT_KEEP_DAYS) or DEFAULT_KEEP_DAYS)
        announce(f"trace: {removed} día(s) de trazas con más de {keep} días retirados")
    day_dir = trace_dir(cfg) / datetime.now().strftime(_DAY_FMT)
    try:
        _mkdir_private(trace_dir(cfg))
        _mkdir_private(day_dir)
        path = day_dir / f"{run_id}.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError:
        return None
    return RunTrace(path, run_id)


# -- lectura (`minka trace`) ----------------------------------------------------------


def list_runs(cfg) -> list[Path]:
    """Trazas existentes, de la más vieja a la más nueva (por mtime)."""
    root = trace_dir(cfg)
    if not root.is_dir():
        return []
    files = [p for p in root.glob("*/*.jsonl") if p.is_file()]
    return sorted(files, key=lambda p: p.stat().st_mtime)


def find_run(cfg, run_id: str) -> Path | None:
    """Acepta el run_id completo o un prefijo único."""
    matches = [p for p in list_runs(cfg) if p.stem.startswith(run_id)]
    if len(matches) == 1:
        return matches[0]
    return None


def read_run(path: Path) -> list[dict]:
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


def clear(cfg) -> int:
    """Borra todas las trazas. Devuelve cuántos archivos retiró."""
    files = list_runs(cfg)
    root = trace_dir(cfg)
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    return len(files)


def format_run(path: Path, events: list[dict]) -> str:
    lines = [f"traza {path.stem} · {path.parent.name} · {len(events)} evento(s)"]
    for e in events:
        kind = e.get("event")
        ts = str(e.get("ts", ""))[11:23]
        if kind == "route":
            extra = f"reglas descartadas: {', '.join(e.get('evaluated') or []) or '-'}"
            notices = e.get("notices") or []
            if notices:
                extra += f" · avisos: {len(notices)}"
            lines.append(f"{ts} route  {e.get('router')} ({e.get('confidence')}) — {e.get('reason')} · {extra}")
        elif kind == "turn":
            lines.append(
                f"{ts} turn   {e.get('agent')} #{e.get('iteration')} · in {e.get('input_tokens')} "
                f"(cache r{e.get('cache_read_input_tokens')}/w{e.get('cache_creation_input_tokens')}) "
                f"out {e.get('output_tokens')} · {e.get('stop_reason')} · {e.get('ms')} ms"
            )
        elif kind == "tool":
            flag = " · BLOQUEADA" if e.get("blocked") else ""
            lines.append(
                f"{ts} tool   {e.get('name')} {json.dumps(e.get('args') or {}, ensure_ascii=False)} → "
                f"{e.get('result_chars')} chars / {e.get('result_lines')} líneas · {e.get('ms')} ms{flag}"
            )
        elif kind == "end":
            lines.append(
                f"{ts} end    ok={e.get('ok')} · {e.get('response_lines')} líneas · "
                f"interrupted={e.get('interrupted')} · {e.get('duration_ms')} ms"
            )
        else:
            lines.append(f"{ts} {kind}")
    return "\n".join(lines)
