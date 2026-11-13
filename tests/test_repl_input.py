"""REPL (core/repl_input.py): pegados colapsados, Alt+Enter, historial en memoria y fallback.

Sin red ni TTY real: prompt_toolkit se maneja con create_pipe_input + DummyOutput.
"""

import io
import os

import pytest
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from typer.testing import CliRunner

from minka import main as minka_main
from minka.config_loader import config
from minka.core import repl_input
from minka.core.repl_input import PasteStore, ReplReader, should_collapse

PASTE_START, PASTE_END, ENTER, ALT_ENTER = "\x1b[200~", "\x1b[201~", "\r", "\x1b\r"
TRES_LINEAS = "Revisa este error:\n  línea 2 del stack\n  línea 3 del stack"


def _console() -> Console:
    return Console(file=io.StringIO(), force_terminal=False, width=120)


@pytest.fixture
def reader():
    with create_pipe_input() as pipe:
        r = ReplReader(
            _console(),
            tty=True,
            collapse_lines=3,
            collapse_chars=500,
            warn_tokens=2000,
            pt_input=pipe,
            pt_output=DummyOutput(),
        )
        r.pipe = pipe
        yield r


def test_umbrales_de_colapso():
    assert should_collapse("a\nb\nc", 3, 500)
    assert not should_collapse("a\nb", 3, 500)
    assert should_collapse("x" * 500, 3, 500)
    assert not should_collapse("x" * 499, 3, 500)


def test_marcadores_numerados_y_expansion():
    store = PasteStore()
    m1 = store.add("uno\ndos\ntres")
    m2 = store.add("a\nb\nc\nd")
    assert (m1, m2) == ("[Pegado #1 +3 líneas]", "[Pegado #2 +4 líneas]")
    assert store.expand(f"mira {m1} y luego {m2}") == "mira uno\ndos\ntres y luego a\nb\nc\nd"
    assert store.expand("[Pegado #9 +1 líneas] no existe") == "[Pegado #9 +1 líneas] no existe"
    assert len(store) == 2


def test_pegado_corto_se_inserta_tal_cual(reader):
    reader.pipe.send_text(f"{PASTE_START}hola mundo{PASTE_END}{ENTER}")
    assert reader.read() == "hola mundo"
    assert len(reader.pastes) == 0


def test_pegado_largo_se_colapsa_y_se_expande_al_enviar(reader):
    reader.pipe.send_text(f"contexto: {PASTE_START}{TRES_LINEAS}{PASTE_END} ¿qué pasa?{ENTER}")
    assert reader.read() == f"contexto: {TRES_LINEAS} ¿qué pasa?"
    assert len(reader.pastes) == 1
    reader.pipe.send_text(f"{PASTE_START}{'y' * 600}{PASTE_END}{ENTER}")
    assert reader.read() == "y" * 600
    assert len(reader.pastes) == 2  # #1 y #2 en la misma sesión


def test_alt_enter_inserta_salto_y_enter_envia(reader):
    reader.pipe.send_text(f"primera{ALT_ENTER}segunda{ENTER}")
    assert reader.read() == "primera\nsegunda"


def test_aviso_de_tamano_sin_interpretar_corchetes(reader):
    reader.warn_tokens = 10  # 3 líneas ≈ 24 tokens estimados
    reader.pipe.send_text(f"{PASTE_START}{TRES_LINEAS}{PASTE_END}{ENTER}")
    reader.read()
    salida = reader.console.file.getvalue()
    assert "[Pegado #1 +3 líneas]" in salida and "tokens estimados" in salida


def test_historial_solo_en_memoria(reader, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert isinstance(reader.session().history, InMemoryHistory)
    reader.pipe.send_text(f"algo{ENTER}")
    reader.read()
    assert os.listdir(tmp_path) == []


def test_sin_tty_usa_console_input(monkeypatch):
    console = _console()
    monkeypatch.setattr(console, "input", lambda prompt: "  analiza algo  ")
    r = ReplReader(console, tty=False, collapse_lines=3, collapse_chars=500, warn_tokens=2000)
    assert r.read() == "analiza algo"
    assert r._session is None  # prompt_toolkit no se construyó


class _Orch:
    last_run_ok = True
    last_usage = None
    last_run_record = None
    memory: list = []

    def __init__(self):
        self.tasks = []

    def run(self, task, **kwargs):
        self.tasks.append(task)
        return f"(stub) {len(task)} chars"

    def clear_session(self):
        self.tasks.append("<clear>")


@pytest.fixture
def repl(monkeypatch):
    orch = _Orch()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-de-prueba")
    monkeypatch.setattr(config, "USE_BEDROCK", False)
    monkeypatch.setattr(minka_main, "_make_orchestrator", lambda: orch)
    monkeypatch.setattr(minka_main, "_print_status", lambda turns, usd=0.0: orch.tasks.append("<status>"))
    return orch


def test_repl_con_tty_envia_el_pegado_como_un_solo_pedido(repl, monkeypatch):
    with create_pipe_input() as pipe:
        reader = ReplReader(
            _console(), tty=True, collapse_lines=3, collapse_chars=500, warn_tokens=2000,
            pt_input=pipe, pt_output=DummyOutput(),
        )
        monkeypatch.setattr(minka_main, "_make_reader", lambda: reader)
        pipe.send_text(f"{PASTE_START}{TRES_LINEAS}{PASTE_END}{ENTER}/clear{ENTER}salir{ENTER}")
        result = CliRunner().invoke(minka_main.app, [])
    assert result.exit_code == 0, result.output
    assert repl.tasks == [TRES_LINEAS, "<clear>"]  # el router/memoria reciben el texto real


def test_repl_sin_tty_conserva_input_y_comandos(repl, monkeypatch):
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    result = CliRunner().invoke(minka_main.app, [], input="/status\nanaliza algo\n/clear\nsalir\n")
    assert result.exit_code == 0, result.output
    assert repl.tasks == ["<status>", "analiza algo", "<clear>"]
    assert "Alt+Enter" not in result.output


def test_umbrales_vienen_de_config(monkeypatch):
    monkeypatch.setattr(config, "PASTE_COLLAPSE_LINES", 7)
    monkeypatch.setattr(config, "PASTE_COLLAPSE_CHARS", 90)
    monkeypatch.setattr(config, "PASTE_WARN_TOKENS", 11)
    monkeypatch.setattr(minka_main, "_is_tty", lambda: False)
    r = minka_main._make_reader()
    assert (r.collapse_lines, r.collapse_chars, r.warn_tokens, r.tty) == (7, 90, 11, False)
    assert repl_input.estimate_tokens("x" * 26) == 10
