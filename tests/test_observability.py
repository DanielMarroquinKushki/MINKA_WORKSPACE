"""Tests offline de observability.trace — no-op seguro y no traga errores.

Las trazas reales (Langfuse + servicio) se validan cuando el servicio esté
arriba. Aquí bloqueamos el contrato de seguridad del context manager.
"""

import pytest

from config_loader import config
from core import observability


def test_trace_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "OBSERVABILITY_ENABLED", False)
    ran = []
    with observability.trace("x") as span:
        assert span is None
        ran.append(1)
    assert ran == [1]


def test_trace_does_not_swallow_body_errors(monkeypatch):
    monkeypatch.setattr(config, "OBSERVABILITY_ENABLED", False)
    with pytest.raises(ValueError):
        with observability.trace("x"):
            raise ValueError("boom")
