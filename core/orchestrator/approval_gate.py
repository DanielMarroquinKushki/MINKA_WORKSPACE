"""
approval_gate — nunca escribe en repos sin confirmación del developer.

Muestra el diff coloreado y espera confirmación explícita.
Nivel 5+: puede auto-aprobar si AUTO_COMMIT está activo (y, a futuro, si los
tests pasan — eso lo cablea el evaluator).

Fix del prompt original: `from rich import prompt` no expone Confirm; se usa
`from rich.prompt import Confirm`.
"""

import difflib
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.prompt import Confirm
from rich.syntax import Syntax

console = Console()


class ApprovalGate:
    def __init__(self, config):
        self.config = config
        self.auto_approve = config.AUTONOMY_LEVEL >= 5 and config.AUTO_COMMIT

    def request(self, file_path: str, new_content: str, reason: str) -> bool:
        """Muestra el diff propuesto y pide aprobación. Devuelve True/False."""
        original = ""
        p = Path(file_path)
        if p.is_file():
            original = p.read_text(encoding="utf-8", errors="replace")

        diff = list(
            difflib.unified_diff(
                original.splitlines(keepends=True),
                new_content.splitlines(keepends=True),
                fromfile=f"original: {file_path}",
                tofile=f"propuesto: {file_path}",
            )
        )

        console.print(
            Panel(
                f"[bold yellow]Cambio propuesto[/bold yellow]\n"
                f"[dim]Archivo:[/dim] {file_path}\n"
                f"[dim]Razón:[/dim] {reason}",
                title="Gate de aprobación",
                border_style="yellow",
            )
        )

        if diff:
            console.print(Syntax("".join(diff), "diff", theme="monokai"))
        else:
            console.print("[dim]Sin cambios en el contenido[/dim]")
            return False  # nada que aprobar

        # Techo de líneas (RDD): advierte si el cambio es grande. NO bloquea.
        lines_added = sum(1 for ln in diff if ln.startswith("+") and not ln.startswith("+++"))
        lines_removed = sum(1 for ln in diff if ln.startswith("-") and not ln.startswith("---"))
        total_changed = lines_added + lines_removed
        line_cap = getattr(self.config, "LINE_CAP", 150)
        if total_changed > line_cap:
            body = (
                f"[bold yellow]⚠️ Cambio grande detectado[/bold yellow]\n"
                f"Líneas modificadas: {total_changed} (límite recomendado: {line_cap})\n"
                f"Considera dividir este cambio en pasos más pequeños."
            )
            hint = getattr(self.config, "TEAM_PILLARS_LINE_CAP_HINT", "")
            if hint:
                body += f"\n{hint}"
            console.print(Panel(body, title="[yellow]Line Cap[/yellow]", border_style="yellow"))

        pillars = getattr(self.config, "TEAM_PILLARS", [])
        if pillars:
            heading = getattr(self.config, "TEAM_PILLARS_HEADING", "Pilares del equipo")
            console.print(
                Panel(
                    f"[bold]Recuerda validar este cambio bajo los {len(pillars)} pilares:[/bold]\n"
                    + "\n".join(pillars),
                    title=f"[cyan]{heading}[/cyan]",
                    border_style="cyan",
                    padding=(0, 1),
                )
            )

        if self.auto_approve:
            console.print("[dim]Auto-aprobado (Nivel 5+ con AUTO_COMMIT)[/dim]")
            approved = True
        else:
            approved = Confirm.ask("\n¿Aplicar este cambio?", default=False)

        if approved:
            self._generate_receipt(file_path, reason, new_content)
        return approved

    def _generate_receipt(self, file_path: str, reason: str, content: str) -> None:
        """Recibo auditable en output/receipts/<fecha>.md (patrón RDD).
        Content-addressed (sha256). Best-effort: nunca rompe el flujo."""
        try:
            import hashlib
            from datetime import datetime

            now = datetime.now()
            date = now.strftime("%Y-%m-%d")
            stamp = now.strftime("%Y-%m-%d %H:%M")
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
            project = getattr(self.config, "ACTIVE_PROJECT", None) or "global"

            entry = (
                f"\n## Cambio aprobado — {stamp}\n"
                f"- **Proyecto:** {project}\n"
                f"- **Archivo:** {file_path}\n"
                f"- **Razón:** {reason}\n"
                f"- **Hash (sha256:12):** {content_hash}\n"
                f"- **Aprobado por:** developer (ApprovalGate)\n"
            )
            receipts_dir = Path(self.config.OUTPUT_DIR) / "receipts"
            receipts_dir.mkdir(parents=True, exist_ok=True)
            path = receipts_dir / f"{date}.md"
            header = "" if path.exists() else f"# Recibos de cambios aprobados — {date}\n"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(header + entry)
        except Exception:  # noqa: BLE001 — best-effort, nunca bloquea
            pass
