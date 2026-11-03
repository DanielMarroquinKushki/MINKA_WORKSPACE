"""pricing — tarifa de lista de Anthropic por modelo y costo de un usage.

Referencia comparable entre modelos, no la factura de Bedrock (las tarifas de
partner son otras). PRICING_VERSION viaja en cada registro de métricas: si la
tabla cambia, los registros viejos siguen siendo interpretables.
"""

PRICING_VERSION = "2026-11"

# USD por millón de tokens: (input, output).
PRICES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
# Modelo más caro en uso: contrafactual "si no hubiera routing".
BASELINE_MODEL = "claude-opus-4-8"

_PREFIXES = ("us.anthropic.", "eu.anthropic.", "apac.anthropic.", "anthropic.")


def normalize_model(model_id: str | None) -> str:
    """'us.anthropic.claude-haiku-4-5-20251001' → 'claude-haiku-4-5'."""
    if not model_id:
        return ""
    base = model_id
    for prefix in _PREFIXES:
        if base.startswith(prefix):
            base = base[len(prefix) :]
            break
    if base in PRICES:
        return base
    for known in PRICES:
        if base.startswith(known):
            return known
    return base


def cost_usd(usage: dict, model: str | None = None) -> float | None:
    """Costo a tarifa de lista; None si el modelo no está tarifado.

    Cache write: 1.25x el input con TTL 5m, 2.00x con 1h; read: 0.10x. Sin el
    desglose por TTL (cache_creation_5m/1h) todo el write se tarifa a 1.25x.
    """
    name = normalize_model(model or usage.get("model"))
    if name not in PRICES:
        return None
    price_in, price_out = PRICES[name]
    write_total = usage.get("cache_creation_input_tokens") or 0
    write_5m = usage.get("cache_creation_5m") or 0
    write_1h = usage.get("cache_creation_1h") or 0
    if write_5m + write_1h == 0:
        cache_write = write_total * price_in * 1.25
    else:
        remainder = max(write_total - write_5m - write_1h, 0)
        cache_write = (write_5m + remainder) * price_in * 1.25 + write_1h * price_in * 2.00
    return (
        (usage.get("input_tokens") or 0) * price_in
        + cache_write
        + (usage.get("cache_read_input_tokens") or 0) * price_in * 0.10
        + (usage.get("output_tokens") or 0) * price_out
    ) / 1_000_000


def baseline_usd(usage: dict) -> float | None:
    """Costo del mismo usage si todo hubiera corrido en BASELINE_MODEL."""
    return cost_usd(usage, BASELINE_MODEL)
