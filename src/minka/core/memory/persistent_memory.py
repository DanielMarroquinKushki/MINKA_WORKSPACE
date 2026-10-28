"""
persistent_memory — memoria persistente entre sesiones (JSON en disco).

Guarda hechos/decisiones que deben sobrevivir al cierre del programa, en
output/logs/persistent_memory.json. Simple y auditable (JSON legible).
"""

import json
from pathlib import Path

from minka.config_loader import config


class PersistentMemory:
    def __init__(self, namespace: str = "global"):
        self.namespace = namespace
        self._path: Path = config.OUTPUT_DIR / "logs" / "persistent_memory.json"
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

    def remember(self, key: str, value) -> None:
        self._data.setdefault(self.namespace, {})[key] = value
        self._save()

    def recall(self, key: str, default=None):
        return self._data.get(self.namespace, {}).get(key, default)

    def all(self) -> dict:
        return self._data.get(self.namespace, {})
