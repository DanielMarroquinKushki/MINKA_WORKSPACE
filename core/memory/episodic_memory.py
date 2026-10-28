"""
episodic_memory — memoria de decisiones/sesiones pasadas (JSON en disco).

Guarda "episodios" de trabajo: (fecha, resumen, tags, metadata) para que los
agentes recuerden qué se hizo en sesiones anteriores. Distinta de
persistent_memory (hechos clave/valor) — esto es historial de experiencia.

Persistencia simple y auditable: output/logs/episodic_memory.json (JSON legible).
Sigue el mismo patrón que persistent_memory (namespaces + _load/_save).
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from config_loader import config


class EpisodicMemory:
    def __init__(self, namespace: str = "global"):
        self.namespace = namespace
        self._path: Path = config.OUTPUT_DIR / "logs" / "episodic_memory.json"
        self._data: dict = self._load()

    def _load(self) -> dict:
        if self._path.is_file():
            try:
                return json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _sessions(self) -> list[dict]:
        return self._data.setdefault(self.namespace, [])

    def add_session_entry(
        self,
        summary: str,
        tags: list[str] | None = None,
        metadata: dict | None = None,
        date: str | None = None,
    ) -> dict:
        """
        Registra una sesión de trabajo. `date` en ISO (YYYY-MM-DD); si no se da,
        usa la fecha UTC actual. Devuelve la entrada creada.
        """
        entry = {
            "date": date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "summary": summary,
            "tags": tags or [],
            "metadata": metadata or {},
        }
        self._sessions().append(entry)
        self._save()
        return entry

    def get_recent_sessions(self, n: int = 5) -> list[dict]:
        """Devuelve las últimas `n` sesiones (más reciente al final del historial)."""
        if n <= 0:
            return []
        return self._sessions()[-n:]

    def search_sessions(self, query: str) -> list[dict]:
        """
        Búsqueda por subcadena (case-insensitive) sobre el resumen y los tags.
        Query vacío devuelve todas las sesiones.
        """
        q = query.strip().lower()
        sessions = self._sessions()
        if not q:
            return list(sessions)
        return [
            s
            for s in sessions
            if q in s.get("summary", "").lower()
            or any(q in str(t).lower() for t in s.get("tags", []))
        ]
