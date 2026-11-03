"""progress — una línea viva en stderr mientras corre un agente.

Muestra agente, vuelta, tool en curso, segundos y tokens reales de la última
respuesta. Solo si stderr es TTY y nadie pidió silencio: --quiet / QUIET_ENV,
--json-out (main apaga config.PROGRESS_ENABLED) o SOURCE_ENV=bridge. Se borra al
terminar (transient) para no ensuciar la salida.
"""

import os
import sys
from time import perf_counter

from rich.console import Console
from rich.live import Live
from rich.text import Text

from minka._app import QUIET_ENV, SOURCE_ENV
from minka.config_loader import config


def enabled() -> bool:
    if os.getenv(QUIET_ENV) or getattr(config, "QUIET", False):
        return False
    if not getattr(config, "PROGRESS_ENABLED", True):
        return False
    if os.getenv(SOURCE_ENV) == "bridge":
        return False
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def format_line(
    agent: str,
    iteration: int,
    status: str,
    seconds: float,
    tokens_in: int | None,
    tokens_out: int | None,
) -> str:
    tokens = (
        f"última respuesta {tokens_in}/{tokens_out} tok"
        if tokens_in is not None
        else "sin respuesta aún"
    )
    return f"{agent} · vuelta {iteration} · {status} · {seconds:.0f}s · {tokens}"


class _Noop:
    def update(self, **kwargs) -> None:
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> bool:
        return False


class Progress:
    def __init__(self, agent: str):
        self.agent = agent
        self.iteration = 0
        self.status = "preparando"
        self.tokens_in: int | None = None
        self.tokens_out: int | None = None
        self.started = perf_counter()
        self._live = Live(
            self, console=Console(stderr=True), transient=True, refresh_per_second=4
        )

    def __rich__(self) -> Text:
        return Text(
            format_line(
                self.agent,
                self.iteration,
                self.status,
                perf_counter() - self.started,
                self.tokens_in,
                self.tokens_out,
            ),
            style="dim",
        )

    def update(
        self,
        iteration: int | None = None,
        status: str | None = None,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
    ) -> None:
        if iteration is not None:
            self.iteration = iteration
        if status is not None:
            self.status = status
        if tokens_in is not None:
            self.tokens_in = tokens_in
            self.tokens_out = tokens_out

    def __enter__(self):
        self._live.__enter__()
        return self

    def __exit__(self, *exc) -> bool:
        self._live.__exit__(*exc)
        return False


def track(agent: str):
    """Context manager con .update(); no-op si el progreso está apagado."""
    return Progress(agent) if enabled() else _Noop()
