"""retention — rotación de agent_runs.jsonl y tamaños de DATA_DIR para `minka doctor`.

No toca las métricas (`output/runs/`): solo el log de corridas de los agentes.
"""

from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_AGENT_LOG_KEEP_DAYS = 30
DEFAULT_SIZE_WARN_MB = 500
SIZE_TARGETS = ((".chromadb", ".chromadb"), ("logs", "output/logs"), ("trace", "output/trace"), ("runs", "output/runs"))


def agent_log_path(cfg) -> Path:
    return Path(getattr(cfg, "OUTPUT_DIR")) / "logs" / "agent_runs.jsonl"


def _entry_ts(line: str) -> datetime | None:
    # El ts es el primer campo que escribe base_agent._log; se lee sin parsear todo el JSON.
    key = '"ts": "'
    i = line.find(key)
    if i < 0:
        return None
    raw = line[i + len(key):].split('"', 1)[0]
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def rotate_agent_runs(cfg, now: datetime | None = None) -> int:
    """Retira las líneas con más de AGENT_LOG_KEEP_DAYS días. Antes de truncar deja una copia
    completa en agent_runs.jsonl.1 (se sobrescribe en cada rotación). Devuelve cuántas retiró."""
    path = agent_log_path(cfg)
    if not path.is_file():
        return 0
    keep = int(getattr(cfg, "AGENT_LOG_KEEP_DAYS", DEFAULT_AGENT_LOG_KEEP_DAYS) or DEFAULT_AGENT_LOG_KEEP_DAYS)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=keep)
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return 0
    kept = []
    removed = 0
    for line in lines:
        ts = _entry_ts(line)
        if ts is not None and ts < cutoff:
            removed += 1
            continue
        kept.append(line)
    if not removed:
        return 0
    try:
        shutil.copy2(path, path.with_name(path.name + ".1"))
        path.write_text("".join(kept), encoding="utf-8")
    except OSError:
        return 0
    return removed


def rotation_notice(removed: int, cfg) -> str:
    keep = int(getattr(cfg, "AGENT_LOG_KEEP_DAYS", DEFAULT_AGENT_LOG_KEEP_DAYS) or DEFAULT_AGENT_LOG_KEEP_DAYS)
    return f"agent_runs.jsonl: {removed} línea(s) con más de {keep} días retiradas (copia previa en agent_runs.jsonl.1)"


def dir_size_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            continue
    return total


def data_dir_sizes(cfg) -> dict[str, int]:
    """Bytes por carpeta de DATA_DIR que crece con el uso: chromadb, logs, trace, runs."""
    data = Path(getattr(cfg, "DATA_DIR"))
    return {label: dir_size_bytes(data / rel) for label, rel in SIZE_TARGETS}


def fmt_mb(n: int) -> str:
    return f"{n / 1_000_000:.1f} MB"
