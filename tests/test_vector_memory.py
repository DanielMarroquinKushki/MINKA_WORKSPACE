"""Tests offline de VectorMemory — contrato de degradación segura.

La memoria real (mem0 + LLM Bedrock) se valida en la tanda SSO. Aquí bloqueamos
que, sin disponibilidad, TODO no-opea sin crashear.
"""

from config_loader import config
from core.memory.vector_memory import VectorMemory


def test_noop_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "VECTOR_MEMORY_ENABLED", False)
    vm = VectorMemory()
    assert vm.add("un recuerdo") is False
    assert vm.search("algo") == []
    assert vm.get_all() == []


def test_noop_when_init_fails(monkeypatch):
    # Habilitado pero mem0 no inicializa (simulado) -> no-op, sin crash.
    monkeypatch.setattr(config, "VECTOR_MEMORY_ENABLED", True)
    vm = VectorMemory()
    monkeypatch.setattr(vm, "_get_mem", lambda: None)
    assert vm.add("x") is False
    assert vm.search("y") == []


def test_unwrap_handles_both_shapes():
    # mem0 devuelve list o {'results': [...]} según versión.
    assert VectorMemory._unwrap({"results": [1, 2]}) == [1, 2]
    assert VectorMemory._unwrap([1, 2]) == [1, 2]
    assert VectorMemory._unwrap(None) == []
