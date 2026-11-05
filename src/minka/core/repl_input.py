"""Lectura de pedidos en el REPL.

Con TTY (stdin y stdout) usa prompt_toolkit: Enter envía, Alt+Enter inserta un salto de
línea y un pegado largo (bracketed paste) se colapsa en el marcador "[Pegado #N +M líneas]",
que se expande al texto real antes de enviarlo. Sin TTY (bridge, tuberías, scripts) se
conserva `console.input` —una línea por `input()`—, que es el comportamiento anterior.
El historial de la sesión vive solo en memoria: nunca se escribe en disco.
"""

from __future__ import annotations

import re
from math import ceil

from minka.core.memory.live_context import CHARS_PER_TOKEN

MARKER_RE = re.compile(r"\[Pegado #(\d+) \+(\d+) líneas?\]")
PROMPT = "> "


def estimate_tokens(text: str) -> int:
    return ceil(len(text) / CHARS_PER_TOKEN)


def should_collapse(text: str, max_lines: int, max_chars: int) -> bool:
    """Se colapsa si tiene al menos `max_lines` líneas o `max_chars` caracteres."""
    return len(text.splitlines()) >= max_lines or len(text) >= max_chars


class PasteStore:
    """Pegados colapsados de la sesión: número → texto real."""

    def __init__(self) -> None:
        self._pastes: dict[int, str] = {}

    @staticmethod
    def marker(number: int, lines: int) -> str:
        return f"[Pegado #{number} +{lines} líneas]"

    def add(self, text: str) -> str:
        number = len(self._pastes) + 1
        self._pastes[number] = text
        return self.marker(number, len(text.splitlines()))

    def expand(self, text: str) -> str:
        """Reemplaza cada marcador por su texto; uno desconocido queda tal cual."""
        return MARKER_RE.sub(lambda m: self._pastes.get(int(m.group(1)), m.group(0)), text)

    def __len__(self) -> int:
        return len(self._pastes)


class ReplReader:
    """Devuelve el pedido ya expandido y sin espacios en los bordes.

    `tty=False` → `console.input` (prompt_toolkit ni se importa). `pt_input` / `pt_output`
    existen para los tests (pipe + DummyOutput); en producción se dejan en None.
    """

    def __init__(
        self,
        console,
        *,
        tty: bool,
        collapse_lines: int,
        collapse_chars: int,
        warn_tokens: int,
        pt_input=None,
        pt_output=None,
    ) -> None:
        self.console = console
        self.tty = tty
        self.collapse_lines = collapse_lines
        self.collapse_chars = collapse_chars
        self.warn_tokens = warn_tokens
        self.pastes = PasteStore()
        self._pt_input = pt_input
        self._pt_output = pt_output
        self._session = None

    def read(self) -> str:
        if not self.tty:
            return self.console.input(f"[bold green]{PROMPT.strip()}[/bold green] ").strip()
        raw = self.session().prompt()
        return self.pastes.expand(raw).strip()

    def session(self):
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _collapse(self, data: str) -> str:
        if not should_collapse(data, self.collapse_lines, self.collapse_chars):
            return data
        marker = self.pastes.add(data)
        tokens = estimate_tokens(data)
        if tokens >= self.warn_tokens:
            self._warn(f"{marker}: ~{tokens:,} tokens estimados ({len(data):,} caracteres)")
        return marker

    def _warn(self, message: str) -> None:
        from prompt_toolkit.application import run_in_terminal

        # markup=False: el marcador lleva corchetes y Rich los tomaría por estilos.
        run_in_terminal(
            lambda: self.console.print(message, markup=False, highlight=False, style="yellow")
        )

    def _build_session(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import InMemoryHistory
        from prompt_toolkit.key_binding import KeyBindings
        from prompt_toolkit.keys import Keys

        bindings = KeyBindings()

        @bindings.add("enter")
        def _send(event) -> None:
            event.current_buffer.validate_and_handle()

        @bindings.add("escape", "enter")
        def _newline(event) -> None:
            event.current_buffer.insert_text("\n")

        @bindings.add(Keys.BracketedPaste)
        def _paste(event) -> None:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            event.current_buffer.insert_text(self._collapse(data))

        extra = {}
        if self._pt_input is not None:
            extra["input"] = self._pt_input
        if self._pt_output is not None:
            extra["output"] = self._pt_output
        return PromptSession(
            message=[("bold fg:ansigreen", PROMPT)],
            history=InMemoryHistory(),
            multiline=True,
            key_bindings=bindings,
            **extra,
        )
