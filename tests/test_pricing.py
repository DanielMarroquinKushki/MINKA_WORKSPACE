"""Tarifa de lista por modelo: normalización de IDs, cache (1.25x/2.00x/0.10x) y baseline."""

import pytest

from minka.core.pricing import (
    BASELINE_MODEL,
    PRICES,
    PRICING_VERSION,
    baseline_usd,
    cost_usd,
    normalize_model,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("us.anthropic.claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("anthropic.claude-sonnet-5", "claude-sonnet-5"),
        ("claude-opus-4-8", "claude-opus-4-8"),
        ("eu.anthropic.claude-opus-5-v1", "claude-opus-5"),
        ("gpt-x", "gpt-x"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_model(raw, expected):
    assert normalize_model(raw) == expected


def test_costo_sin_cache():
    usage = {"model": "claude-sonnet-5", "input_tokens": 1_000_000, "output_tokens": 100_000}
    assert cost_usd(usage) == pytest.approx(2.0 + 1.0)


def test_cache_sin_desglose_write_1_25x_read_0_10x():
    usage = {
        "model": "us.anthropic.claude-opus-4-8-20250101",
        "cache_creation_input_tokens": 1_000_000,
        "cache_read_input_tokens": 1_000_000,
    }
    assert cost_usd(usage) == pytest.approx(5.0 * 1.25 + 5.0 * 0.10)


def test_cache_con_desglose_5m_y_1h():
    usage = {
        "model": "claude-opus-4-8",
        "cache_creation_input_tokens": 300_000,
        "cache_creation_5m": 100_000,
        "cache_creation_1h": 100_000,
    }
    # El resto sin explicar (100k) se tarifa al multiplicador barato.
    assert cost_usd(usage) == pytest.approx((200_000 * 5.0 * 1.25 + 100_000 * 5.0 * 2.0) / 1e6)


def test_modelo_sin_tarifa_devuelve_none():
    assert cost_usd({"model": "gpt-x", "input_tokens": 5}) is None
    assert cost_usd({}) is None
    assert baseline_usd({"input_tokens": 5}) is not None  # el baseline siempre está tarifado


def test_baseline_es_el_modelo_mas_caro_y_hay_version():
    usage = {"model": "claude-haiku-4-5", "input_tokens": 1_000_000, "output_tokens": 1_000}
    assert BASELINE_MODEL in PRICES
    assert baseline_usd(usage) == pytest.approx(cost_usd(usage, BASELINE_MODEL))
    assert baseline_usd(usage) > cost_usd(usage)
    assert isinstance(PRICING_VERSION, str) and PRICING_VERSION
