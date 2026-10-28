"""Tests de core.structured.call_structured — con clientes LLM fake (sin red)."""
from types import SimpleNamespace

from pydantic import BaseModel

from minka.core.structured import call_structured


class Demo(BaseModel):
    decision: str
    items: list[str]


def _client_returning(input_dict):
    """Cliente fake que devuelve un bloque tool_use con `input_dict`."""

    class Msgs:
        def create(self, **kw):
            blk = SimpleNamespace(
                type="tool_use", name=kw["tool_choice"]["name"], input=input_dict
            )
            return SimpleNamespace(content=[blk])

    return SimpleNamespace(messages=Msgs())


def _call(client):
    return call_structured(
        client, "m", "sys", [{"role": "user", "content": "x"}],
        Demo, tool_name="submit", tool_description="demo",
    )


def test_happy_path_returns_validated_model():
    r = _call(_client_returning({"decision": "aprobar", "items": ["a", "b"]}))
    assert isinstance(r, Demo)
    assert r.decision == "aprobar"
    assert r.items == ["a", "b"]


def test_invalid_input_returns_none():
    # falta 'items' -> ValidationError -> None
    assert _call(_client_returning({"decision": "x"})) is None


def test_exception_returns_none():
    class Msgs:
        def create(self, **kw):
            raise RuntimeError("network")

    assert _call(SimpleNamespace(messages=Msgs())) is None


def test_no_tool_use_block_returns_none():
    class Msgs:
        def create(self, **kw):
            return SimpleNamespace(content=[SimpleNamespace(type="text", text="hola")])

    assert _call(SimpleNamespace(messages=Msgs())) is None