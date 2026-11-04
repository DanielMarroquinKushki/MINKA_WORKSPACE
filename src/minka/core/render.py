"""render — salida legible del resultado del agente (Markdown con rich), como Claude Code.

Renderiza solo cuando hay alguien mirando: stdout TTY, sin --quiet, sin --json-out, sin
--plain / MINKA_PLAIN=1 y sin MINKA_SOURCE=bridge. En cualquier otro caso imprime el texto
crudo con `markup=False` (los corchetes como `[rag]` salen literales también en Markdown:
rich no interpreta su propio markup dentro de un Markdown).

El texto que se guarda en memoria, en el historial y en --json-out es siempre el crudo:
aquí solo se decide cómo se imprime.

Los encabezados h1/h2 van alineados a la izquierda (rich centra el h1 por defecto).

Limitación conocida: Markdown interpreta `__x__` o `**x**` sueltos como negrita (un
`__init__` sin backticks sale como "init"); los guiones bajos intra-palabra y los
asteriscos sin cierre quedan literales. Si una respuesta se ve rara, `--plain` o
MINKA_PLAIN=1 imprimen el texto tal cual.
"""

from __future__ import annotations

import os
import sys

from rich.console import Console
from rich.markdown import Heading, Markdown
from rich.text import Text

from minka._app import PLAIN_ENV, SOURCE_ENV


class LeftHeading(Heading):
    """Encabezado a la izquierda: h1 en negrita subrayada, el resto en negrita."""

    def __rich_console__(self, console, options):
        text = self.text
        text.justify = "left"
        text.stylize("bold underline" if self.tag == "h1" else "bold")
        yield text


class PlainMarkdown(Markdown):
    elements = {**Markdown.elements, "heading_open": LeftHeading}


def should_render(*, quiet: bool = False, json_out: bool = False, plain: bool = False) -> bool:
    if quiet or json_out or plain:
        return False
    if os.getenv(PLAIN_ENV, "").strip().lower() in ("1", "true", "yes"):
        return False
    if os.getenv(SOURCE_ENV) == "bridge":
        return False
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


def print_result(text: str, console: Console, *, render: bool, rule: bool = True) -> None:
    """Imprime la respuesta separada del ruido previo (tools, progreso) por una regla fina."""
    if rule and render:
        console.rule(style="dim")
    if render:
        console.print(PlainMarkdown(text or ""))
    else:
        console.print(text or "", markup=False)


def dim_line(text: str) -> Text:
    """Línea atenuada sin interpretar corchetes (nombres de agente, pedidos)."""
    return Text(text, style="dim")
