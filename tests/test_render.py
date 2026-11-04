"""Salida legible (core/render.py): Markdown con rich solo cuando hay TTY y nadie pidió
texto crudo. Se captura con Console(record=True); sin red ni modelo."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from minka._app import PLAIN_ENV, QUIET_ENV, SOURCE_ENV
from minka.core import render

SAMPLE = Path(__file__).parent / "fixtures" / "demo" / "render_sample.md"


def _console(width: int = 80) -> Console:
    return Console(record=True, width=width, force_terminal=True, color_system="truecolor", file=io.StringIO())


def _render(text: str, width: int = 80) -> tuple[str, str]:
    """(texto plano exportado, texto con ANSI) de la fixture renderizada."""
    console = _console(width)
    render.print_result(text, console, render=True)
    return console.export_text(clear=False), console.export_text(styles=True)


def test_corchetes_literales_y_sin_markup_de_rich():
    plain, _ = _render(SAMPLE.read_text(encoding="utf-8"))
    assert "[rag]" in plain
    assert "[bold]texto[/bold]" in plain


def test_identificadores_con_guion_bajo_y_asterisco_sobreviven():
    """Guiones bajos intra-palabra y asteriscos sin cierre quedan literales. El único caso
    que Markdown se come es `__x__` suelto (negrita): en backticks sobrevive, y con
    --plain / MINKA_PLAIN=1 sale todo crudo (documentado en core/render.py)."""
    plain, _ = _render(SAMPLE.read_text(encoding="utf-8"))
    for ident in ("snake_case_name", "*args", "**kwargs", "a_b*c", "__init__"):
        assert ident in plain, ident
    # Sueltos y aislados en la línea también sobreviven (un solo * o _ no cierra énfasis).
    for ident in ("snake_case_name", "*args", "**kwargs", "a_b*c"):
        bare, _ = _render(f"usa {ident} aquí")
        assert ident in bare, ident
    # Limitación conocida: `__x__` suelto es negrita, y varios * en la misma línea se emparejan.
    bare, _ = _render("suelto __init__ fin")
    assert "init" in bare and "__init__" not in bare  # → --plain / MINKA_PLAIN=1


def test_titulos_a_la_izquierda_sin_centrar():
    plain, _ = _render("# Título h1\n## Sub h2\n### h3\ntexto", width=60)
    lines = [ln for ln in plain.splitlines() if ln.strip() and not ln.strip().startswith("─")]
    assert lines[0].startswith("Título h1")
    assert lines[1].startswith("Sub h2")
    assert lines[2].startswith("h3")


def test_bloque_de_codigo_con_pygments_y_tabla_dentro_del_ancho():
    plain, ansi = _render(SAMPLE.read_text(encoding="utf-8"), width=72)
    assert "func buildTokenProvider" in plain
    assert "\x1b[38;2;" in ansi  # colores truecolor de pygments en el bloque go
    assert all(len(ln) <= 72 for ln in plain.splitlines())
    assert "Proveedor SSO" in plain and "gateway/sender.go:71" in plain


@pytest.mark.parametrize("width", [100, 78, 60])
def test_tabla_sin_filas_duplicadas_ni_citas_repetidas(width):
    """Cada fila de la tabla sale una sola vez y su cita no se repite en la prosa; en anchos
    menores la celda se parte en dos líneas, pero no se duplica."""
    plain, _ = _render(SAMPLE.read_text(encoding="utf-8"), width=width)
    for row in ("Proveedor estático", "Proveedor SSO", "Uso en el sender"):
        assert plain.count(row) == 1, (width, row)
    # A 60 columnas la celda de la cita se parte en dos líneas: nunca más de una vez.
    expected = 1 if width >= 78 else 0
    assert plain.count("gateway/sender.go:71") == expected
    assert plain.count("service/token_provider.go:75") == expected


def test_tabla_angosta_parte_celdas_sin_duplicar_filas():
    plain, _ = _render(SAMPLE.read_text(encoding="utf-8"), width=40)
    assert plain.count("Proveedor SSO") <= 1 and plain.count("gateway/sender.go:71") <= 1
    assert "header" in plain and "sender" in plain  # la fila sigue, aunque partida


def test_anotaciones_y_nota_acotada_se_mantienen():
    plain, _ = _render(SAMPLE.read_text(encoding="utf-8"))
    assert "service/missing_file.go:10 ⚠ no existe" in plain
    assert "Respuesta acotada; pide 'en detalle' para ampliar." in plain


def test_regla_fina_separa_la_respuesta():
    plain, _ = _render("texto", width=40)
    assert plain.splitlines()[0].strip().startswith("─")


def test_modo_crudo_no_interpreta_ni_dibuja():
    console = _console(60)
    render.print_result("# Título\n[rag] y `x`", console, render=False)
    out = console.export_text()
    assert "# Título" in out and "[rag] y `x`" in out
    assert "─" not in out


@pytest.fixture
def tty(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True, raising=False)
    for var in (PLAIN_ENV, QUIET_ENV, SOURCE_ENV):
        monkeypatch.delenv(var, raising=False)


def test_should_render_solo_con_tty_y_sin_flags(tty, monkeypatch):
    import sys

    assert render.should_render() is True
    assert render.should_render(quiet=True) is False
    assert render.should_render(json_out=True) is False
    assert render.should_render(plain=True) is False
    monkeypatch.setenv(PLAIN_ENV, "1")
    assert render.should_render() is False
    monkeypatch.delenv(PLAIN_ENV)
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    assert render.should_render() is False
    monkeypatch.delenv(SOURCE_ENV)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False, raising=False)
    assert render.should_render() is False


def test_dim_line_no_interpreta_corchetes():
    console = _console(60)
    console.print(render.dim_line("architecture_agent · [rag] pedido"))
    assert "[rag] pedido" in console.export_text()
