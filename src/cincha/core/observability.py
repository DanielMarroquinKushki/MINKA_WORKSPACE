"""
observability — trazas del loop de agentes con Langfuse (opcional, Nivel 4+).

Guard: config.OBSERVABILITY_ENABLED + credenciales Langfuse en el entorno
(LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY / LANGFUSE_HOST). Si está off o no
configurado, `trace()` es un no-op (yield None) — cero overhead y cero riesgo
para el loop del agente. NUNCA traga las excepciones del cuerpo trazado (para no
ocultar errores del agente); solo guarda los fallos de la PROPIA observabilidad.

Requiere un servicio Langfuse (self-host Docker o cloud) para VER las trazas; el
wiring y las trazas reales se validan cuando el servicio esté disponible.
"""

from contextlib import contextmanager

from cincha.config_loader import config

_client = None
_init_failed = False


def _get_client():
    """Lazy init del cliente Langfuse. None si desactivado o no configurable."""
    global _client, _init_failed
    if _client is not None:
        return _client
    if _init_failed or not getattr(config, "OBSERVABILITY_ENABLED", False):
        return None
    try:
        from langfuse import Langfuse

        _client = Langfuse()  # lee LANGFUSE_* del entorno
        return _client
    except Exception:  # noqa: BLE001 — sin lib/keys: no-op
        _init_failed = True
        return None


@contextmanager
def trace(name: str, **metadata):
    """
    Abre un span Langfuse si está disponible; si no, no-op (yield None).

    Las excepciones del cuerpo trazado se propagan normalmente (no se tragan) —
    solo se guardan los fallos de la observabilidad en sí.
    """
    client = _get_client()
    if client is None:
        yield None
        return
    span = None
    try:
        span = client.start_span(name=name, metadata=metadata or None)
    except Exception:  # noqa: BLE001 — no se pudo crear el span → sigue sin trazar
        span = None
    try:
        yield span
    finally:
        try:
            if span is not None:
                span.end()
            client.flush()
        except Exception:  # noqa: BLE001 — un fallo al cerrar no afecta al agente
            pass
