"""budget — tope de gasto por sesión, freno de ráfaga y confirmación del estimado.

El orquestador no lee stdin: cuando una corrida no debe lanzarse levanta `BudgetGate`
(kind, usd, n) ANTES de crear el agente y quien lo llama decide. En el REPL con TTY se
pregunta: un "sí" sube el tope un escalón (`allow`) o reinicia la ráfaga; un "no" descarta
el pedido. Sin TTY o con `MINKA_SOURCE=bridge`: una línea y exit 3. Las capas de costo $0
(saludo, estado, meta, ambiguo, sin cambios) nunca pasan por aquí.

Ráfaga: BURST_N corridas CON AGENTE consecutivas cuyo inicio ocurre a menos de
BURST_GAP_SECONDS del fin de la anterior (el pegado partido en varias líneas); una persona
rápida no la dispara. El reloj es inyectable (`clock`) para probar sin esperar.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from minka.config_loader import config

KIND_SESSION = "sesion"
KIND_BURST = "rafaga"
KIND_ESTIMATE = "estimado"
# Orígenes que nunca pasan por el gate: cada corrida de `evals quality` crea su Orchestrator
# y el bridge lanza un proceso por tarea (sin sesión que proteger ni humano que pregunte).
EXEMPT_SOURCES = ("bridge", "eval_quality")
EXIT_CODE = 3


def session_max_usd() -> float:
    return float(getattr(config, "SESSION_MAX_USD", 5.0) or 0)


def burst_n() -> int:
    return int(getattr(config, "BURST_N", 3) or 0)


def burst_gap_seconds() -> float:
    return float(getattr(config, "BURST_GAP_SECONDS", 5) or 0)


def estimate_confirm_usd() -> float:
    return float(getattr(config, "ESTIMATE_CONFIRM_USD", 0.50) or 0)


class BudgetGate(Exception):
    """La corrida no se lanza hasta que alguien decida. `usd`: gasto de la sesión (sesion,
    rafaga) o estimado del pedido (estimado); `n`: corridas de la sesión, corridas en ráfaga
    o corridas detrás de la mediana."""

    def __init__(self, kind: str, usd: float, n: int, ceiling: float = 0.0):
        self.kind, self.usd, self.n, self.ceiling = kind, float(usd), int(n), float(ceiling)
        super().__init__(self.line())

    def line(self) -> str:
        if self.kind == KIND_SESSION:
            return (
                f"Tope de sesión: ${self.usd:.2f} gastados en {self.n} corrida(s), "
                f"tope ${self.ceiling:.2f} (SESSION_MAX_USD)."
            )
        if self.kind == KIND_BURST:
            return (
                f"Ráfaga: {self.n} corridas con agente seguidas a menos de "
                f"{burst_gap_seconds():g} s una de otra (BURST_N={burst_n()}); ${self.usd:.2f} en la sesión."
            )
        return (
            f"Estimado ~${self.usd:.2f} (mediana de {self.n} corridas); sesión ${self.ceiling:.2f} de tope."
        )

    def question(self) -> str:
        if self.kind == KIND_SESSION:
            return f"¿Subir el tope a ${self.ceiling + session_max_usd():.2f} y seguir?"
        if self.kind == KIND_BURST:
            return "¿Seguir y reiniciar el contador de ráfaga?"
        return "¿Lanzar igual?"


@dataclass
class BudgetState:
    """Gasto de la sesión y ráfaga. `clock` devuelve segundos monótonos."""

    clock: callable = field(default=time.monotonic)
    session_usd: float = 0.0
    runs: int = 0  # corridas con costo registradas en la sesión
    ceiling: float = field(default_factory=session_max_usd)
    estimate_ok_usd: float = field(default_factory=estimate_confirm_usd)
    burst: int = 0  # corridas con agente seguidas dentro del hueco
    last_agent_end: float | None = None

    def before_agent(self) -> None:
        """Llamar justo antes de crear el primer agente de la corrida. Levanta BudgetGate."""
        if self.ceiling > 0 and self.session_usd >= self.ceiling:
            raise BudgetGate(KIND_SESSION, self.session_usd, self.runs, self.ceiling)
        now = self.clock()
        gap = burst_gap_seconds()
        if self.last_agent_end is not None and gap > 0 and now - self.last_agent_end < gap:
            self.burst += 1
        else:
            self.burst = 0
        n = burst_n()
        if n > 0 and self.burst >= n:
            self.burst = n - 1  # un "no" deja el contador donde estaba; un "sí" lo reinicia
            raise BudgetGate(KIND_BURST, self.session_usd, n, self.ceiling)

    def check_estimate(self, estimate: float | None, n: int) -> None:
        """Con estimado: confirmar si supera ESTIMATE_CONFIRM_USD o rompería el tope."""
        if estimate is None:
            return
        over_ceiling = self.ceiling > 0 and self.session_usd + estimate > self.ceiling
        if estimate > self.estimate_ok_usd or over_ceiling:
            raise BudgetGate(KIND_ESTIMATE, estimate, n, self.session_usd)

    def after_run(self, cost_usd: float | None, had_agent: bool) -> None:
        """Al cerrar cada corrida: suma el costo (planner e interrumpidas incluidos)."""
        if cost_usd:
            self.session_usd += float(cost_usd)
            self.runs += 1
        if had_agent:
            self.last_agent_end = self.clock()

    def allow(self, gate: BudgetGate) -> str:
        """El dev dijo "sí": sube el tope un escalón, reinicia la ráfaga o acepta el estimado.
        Devuelve una línea con lo que cambió."""
        if gate.kind == KIND_SESSION:
            self.ceiling += session_max_usd()
            return f"Tope de sesión ahora ${self.ceiling:.2f}."
        if gate.kind == KIND_BURST:
            self.burst = 0
            self.last_agent_end = None
            return "Contador de ráfaga reiniciado."
        self.estimate_ok_usd = max(self.estimate_ok_usd, gate.usd)
        if self.ceiling > 0 and self.session_usd + gate.usd > self.ceiling:
            self.ceiling += session_max_usd()
            return f"Estimado aceptado; tope de sesión ahora ${self.ceiling:.2f}."
        return f"Estimados hasta ${self.estimate_ok_usd:.2f} ya no preguntan en esta sesión."


def estimate_line(estimate: float | None, n: int) -> str:
    """Texto de la línea de decisión: nunca un rango ni un factor de caché inventados."""
    if estimate is None:
        return "sin estimado"
    return f"estimado ~${estimate:.2f} (mediana de {n} corridas)"
