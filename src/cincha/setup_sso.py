"""
setup_sso.py — Configuración y verificación del perfil AWS SSO para Bedrock.

Se corre UNA SOLA VEZ para dejar el perfil listo:

    python setup_sso.py            # crea/verifica el perfil "bedrock"
    python setup_sso.py --verify   # solo verifica que todo funciona

Qué hace (sin --verify):
    1. Si el perfil no existe en ~/.aws/config → lanza `aws configure sso`.
    2. Si ya existe → valida con sts.get_caller_identity().
    3. Muestra minutos restantes del token SSO + sso_account_id / sso_role_name.

Con --verify: corre el checklist completo y hace un ping real a Bedrock.

No hardcodea credenciales ni el sso_start_url (ese vive en ~/.aws/config).
"""

from __future__ import annotations

import subprocess

import typer
from rich.console import Console

from cincha.aws_session_manager import AWSSessionManager
from cincha.config_loader import config

app = typer.Typer(add_completion=False)
console = Console()

PROFILE = getattr(config, "AWS_PROFILE", "bedrock")
REGION = getattr(config, "AWS_REGION", "us-east-1")

# Modelo del ping de verificación (smoke-test de conectividad, independiente de
# config.MODELS). Usa el prefijo configurable: los modelos on-demand de Bedrock
# se invocan vía inference profile regional (p.ej. "us.anthropic.").
_PING_MODEL = f"{getattr(config, 'BEDROCK_MODEL_PREFIX', 'us.anthropic.')}claude-sonnet-4-6"


def _profile_exists(profile: str) -> bool:
    """True si el perfil aparece en la config del AWS CLI."""
    try:
        result = subprocess.run(
            ["aws", "configure", "list-profiles"],
            capture_output=True,
            text=True,
            check=True,
        )
        return profile in result.stdout.split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def _shell_profile_warning() -> None:
    """Advierte si el shell exporta un AWS_PROFILE distinto (p.ej. 'default')
    que dispara USE_BEDROCK con un perfil sin acceso a Bedrock."""
    import os

    shell_profile = os.getenv("AWS_PROFILE")
    if shell_profile and shell_profile != "bedrock":
        console.print(
            f"\n[yellow]⚠️  AWS_PROFILE={shell_profile} detectado en el entorno del shell.[/yellow]\n"
            "    Para usar Bedrock correctamente: [cyan]export AWS_PROFILE=bedrock[/cyan]\n"
            "    Para modo API directa:          [cyan]export LLM_BACKEND=anthropic[/cyan]"
        )


@app.command()
def main(
    verify: bool = typer.Option(False, "--verify", help="Solo verificar, no configurar."),
) -> None:
    """Configura (o verifica) el perfil AWS SSO para Bedrock."""
    if verify:
        _run_verify()
        return

    console.rule("[bold]Configuración AWS SSO — perfil 'bedrock'[/bold]")

    if not _profile_exists(PROFILE):
        console.print(
            f"\nEl perfil [cyan]{PROFILE}[/cyan] no existe todavía.\n"
            "Se abrirá el asistente del AWS CLI. Cuando te pregunte:\n"
            "  • [bold]SSO start URL[/bold]: pega la de tu organización.\n"
            "  • [bold]SSO region[/bold]: la región de tu SSO (p.ej. us-east-1).\n"
            "  • [bold]CLI default client Region[/bold]: "
            f"[cyan]{REGION}[/cyan]\n"
            f"  • [bold]CLI profile name[/bold]: [cyan]{PROFILE}[/cyan]\n"
        )
        try:
            subprocess.run(["aws", "configure", "sso", "--profile", PROFILE], check=True)
        except FileNotFoundError:
            console.print("[red]✗ No se encontró el AWS CLI. Instálalo y reintenta.[/red]")
            raise typer.Exit(code=1)
        except subprocess.CalledProcessError:
            console.print("[red]✗ La configuración SSO no se completó.[/red]")
            raise typer.Exit(code=1)
    else:
        console.print(f"[green]✓[/green] El perfil [cyan]{PROFILE}[/cyan] ya existe.")

    # Verificación tras configurar.
    _run_verify()


def _run_verify() -> None:
    """Checklist completo de verificación."""
    console.rule("[bold]Verificación del entorno Bedrock/SSO[/bold]")
    mgr = AWSSessionManager(
        profile_name=PROFILE,
        region=REGION,
        refresh_before_minutes=getattr(config, "BEDROCK_REFRESH_BEFORE_MINUTES", 5),
    )
    ok = True

    # 1 — Perfil configurado.
    if _profile_exists(PROFILE):
        console.print(f"[green]✓[/green] Perfil '{PROFILE}' configurado")
    else:
        console.print(
            f"[red]✗[/red] Perfil '{PROFILE}' NO configurado — corre: python setup_sso.py"
        )
        _shell_profile_warning()
        raise typer.Exit(code=1)

    # 2 — Token SSO válido + datos del perfil.
    status = mgr.sso_token_status()
    if status["valid"]:
        console.print(
            f"[green]✓[/green] Token SSO válido (expira en {status['minutes_remaining']} minutos)"
        )
    else:
        console.print(
            f"[red]✗[/red] Token SSO expirado o ausente — corre: [cyan]aws sso login --profile {PROFILE}[/cyan]"
        )
        ok = False
    console.print(f"    sso_account_id: [cyan]{status['sso_account_id']}[/cyan]")
    console.print(f"    sso_role_name:  [cyan]{status['sso_role_name']}[/cyan]")

    # 3 — Credenciales de rol (dispara Capas 1-2 de botocore).
    try:
        ident = mgr.get_client("sts").get_caller_identity()
        console.print(
            f"[green]✓[/green] Credenciales de rol obtenidas correctamente (cuenta {ident['Account']})"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] No se pudieron obtener credenciales de rol: {exc}")
        _shell_profile_warning()
        raise typer.Exit(code=1)

    # 4 — Manager inicializado.
    console.print("[green]✓[/green] AWSSessionManager inicializado")

    # 5 — Cliente Bedrock construible.
    try:
        import anthropic

        client = anthropic.AnthropicBedrock(aws_profile=PROFILE, aws_region=REGION)
        console.print("[green]✓[/green] Cliente Bedrock listo")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] No se pudo crear el cliente Bedrock: {exc}")
        raise typer.Exit(code=1)

    # 6 — Auto-refresh configurado (informativo).
    console.print(
        f"[green]✓[/green] Auto-refresh configurado "
        f"(renueva {mgr.refresh_before_minutes} min antes de expirar)"
    )

    # 7 — Ping real a Bedrock (no solo credenciales: que el modelo responda).
    try:
        client.messages.create(
            model=_PING_MODEL,
            max_tokens=10,
            messages=[{"role": "user", "content": "ping"}],
        )
        console.print("[green]✓[/green] Bedrock responde correctamente")
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]✗[/red] Bedrock no respondió: {exc}")
        _shell_profile_warning()
        raise typer.Exit(code=1)

    if ok:
        console.print("\n[bold green]Todo listo para el agente.[/bold green]")
    else:
        _shell_profile_warning()
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
