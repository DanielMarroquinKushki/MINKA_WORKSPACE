"""
session_memory — memoria de la sesión actual (in-memory, se pierde al salir).

Guarda el historial de intercambios (tarea → resultado) de un agente para dar
continuidad dentro de una misma sesión de conversación.
"""


class SessionMemory:
    def __init__(self, agent_name: str, max_exchanges: int = 20):
        self.agent_name = agent_name
        self.max_exchanges = max_exchanges
        self._exchanges: list[dict] = []

    def get_context(self) -> list[dict]:
        """
        Devuelve el historial como lista de mensajes (formato Messages API):
        pares user/assistant. Se acota a los últimos `max_exchanges`.
        """
        messages: list[dict] = []
        for ex in self._exchanges[-self.max_exchanges :]:
            messages.append({"role": "user", "content": ex["task"]})
            messages.append({"role": "assistant", "content": ex["result"]})
        return messages

    def add_exchange(self, task: str, result: str) -> None:
        self._exchanges.append({"task": task, "result": result})

    def clear(self) -> None:
        self._exchanges.clear()

    def __len__(self) -> int:
        return len(self._exchanges)
