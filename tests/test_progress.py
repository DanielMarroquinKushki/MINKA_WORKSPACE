"""Indicador de progreso: solo con stderr TTY y sin --quiet / QUIET_ENV / --json-out / bridge."""

import sys

from minka._app import QUIET_ENV, SOURCE_ENV
from minka.config_loader import config
from minka.core import progress


def _tty(monkeypatch, value):
    monkeypatch.setattr(sys, "stderr", type("S", (), {"isatty": lambda self: value, "write": lambda *a: None, "flush": lambda *a: None})())


def _base(monkeypatch):
    monkeypatch.delenv(QUIET_ENV, raising=False)
    monkeypatch.delenv(SOURCE_ENV, raising=False)
    monkeypatch.setattr(config, "QUIET", False, raising=False)
    monkeypatch.setattr(config, "PROGRESS_ENABLED", True, raising=False)


def test_enabled_solo_con_tty(monkeypatch):
    _base(monkeypatch)
    _tty(monkeypatch, False)
    assert progress.enabled() is False
    _tty(monkeypatch, True)
    assert progress.enabled() is True


def test_apagado_por_quiet_json_out_y_bridge(monkeypatch):
    _base(monkeypatch)
    _tty(monkeypatch, True)
    monkeypatch.setenv(QUIET_ENV, "1")
    assert progress.enabled() is False
    monkeypatch.delenv(QUIET_ENV)
    monkeypatch.setattr(config, "QUIET", True, raising=False)
    assert progress.enabled() is False
    monkeypatch.setattr(config, "QUIET", False, raising=False)
    monkeypatch.setattr(config, "PROGRESS_ENABLED", False, raising=False)  # main: --json-out
    assert progress.enabled() is False
    monkeypatch.setattr(config, "PROGRESS_ENABLED", True, raising=False)
    monkeypatch.setenv(SOURCE_ENV, "bridge")
    assert progress.enabled() is False


def test_track_es_noop_sin_tty(monkeypatch):
    _base(monkeypatch)
    _tty(monkeypatch, False)
    with progress.track("x") as p:
        p.update(iteration=1, status="tool read_file", tokens_in=10, tokens_out=2)
    assert isinstance(p, progress._Noop)


def test_format_line():
    assert (
        progress.format_line("architecture_agent", 3, "tool read_file", 12.4, 1384, 171)
        == "architecture_agent · vuelta 3 · tool read_file · 12s · última respuesta 1384/171 tok"
    )
    assert progress.format_line("a", 1, "esperando al modelo", 0.2, None, None).endswith("sin respuesta aún")


def test_progress_actualiza_estado_sin_live(monkeypatch):
    p = progress.Progress.__new__(progress.Progress)
    p.agent, p.iteration, p.status, p.tokens_in, p.tokens_out, p.started = "a", 0, "preparando", None, None, 0.0
    p.update(iteration=2, status="tool x", tokens_in=5, tokens_out=1)
    text = p.__rich__().plain
    assert text.startswith("a · vuelta 2 · tool x · ") and text.endswith("última respuesta 5/1 tok")
