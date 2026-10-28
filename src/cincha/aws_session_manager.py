"""
aws_session_manager.py — Manejo de sesiones AWS SSO para Amazon Bedrock.

Problema que resuelve: la sesión SSO expira (~40 min) y el agente se queda
sin credenciales a mitad de una tarea larga. Este módulo mantiene la sesión
viva con 3 capas de renovación, en orden de preferencia:

  Capa 1 — RefreshableCredentials (botocore)
    Delegada al SDK. Al crear una boto3.Session con un perfil SSO, botocore
    ya renueva las credenciales de rol automáticamente ANTES de que expiren,
    leyendo el token SSO del cache local. No reimplementamos esto: el propio
    SDK expone las credenciales como RefreshableCredentials y llama al refresh
    sin intervención del usuario.

  Capa 2 — SSO Cache
    También delegada al SDK. Cuando las credenciales de rol caducan, botocore
    lee el accessToken vigente de ~/.aws/sso/cache/*.json y vuelve a llamar a
    sso.get_role_credentials() SIN abrir el browser. Sucede de forma
    transparente dentro de get_caller_identity() / cualquier llamada AWS.

  Capa 3 — Pausa elegante  (LO QUE SÍ construimos aquí)
    Si el token SSO también expiró (las dos capas anteriores fallan), NO
    crasheamos el agente. Mostramos instrucciones, esperamos hasta 10 minutos
    verificando cada 30s, y retomamos en cuanto el usuario renueve. Si pasa el
    tiempo, salimos limpiamente.

Uso:
    mgr = AWSSessionManager(profile_name="bedrock", region="us-east-1")
    mgr.get_bedrock_client()        # cliente bedrock-runtime listo para usar
    mgr.get_client("s3")            # cualquier cliente AWS
    mgr.ensure_valid_session()      # verifica antes de una tarea larga → bool

Seguridad:
    - No hardcodea credenciales, tokens ni secrets.
    - No contiene el sso_start_url (vive en ~/.aws/config del usuario).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import boto3
from rich.console import Console

console = Console()

# Directorio donde el AWS CLI cachea el token SSO (accessToken vigente).
_SSO_CACHE_DIR = Path.home() / ".aws" / "sso" / "cache"


class AWSSessionManager:
    """Gestiona una sesión AWS basada en un perfil SSO, con renovación
    automática (Capas 1-2 vía botocore) y pausa elegante (Capa 3 propia)."""

    def __init__(
        self,
        profile_name: str = "bedrock",
        region: str = "us-east-1",
        refresh_before_minutes: int = 5,
    ) -> None:
        self.profile_name = profile_name
        self.region = region
        # Margen (min) con el que consideramos que un token está "por vencer".
        # botocore renueva solo; esto es informativo para sso_token_status().
        self.refresh_before_minutes = refresh_before_minutes

        self._session: Optional[boto3.Session] = None
        self._clients: dict[str, Any] = {}

    # -- Sesión / clientes -----------------------------------------------------
    @property
    def session(self) -> boto3.Session:
        """boto3.Session cacheada para el perfil SSO. botocore adjunta aquí las
        RefreshableCredentials (Capa 1) que se renuevan solas."""
        if self._session is None:
            self._session = boto3.Session(
                profile_name=self.profile_name,
                region_name=self.region,
            )
        return self._session

    def get_client(self, service_name: str) -> Any:
        """Devuelve un cliente boto3 del servicio pedido (cacheado)."""
        if service_name not in self._clients:
            self._clients[service_name] = self.session.client(service_name, region_name=self.region)
        return self._clients[service_name]

    def get_bedrock_client(self) -> Any:
        """Cliente bedrock-runtime listo para invocar modelos."""
        return self.get_client("bedrock-runtime")

    def _reset(self) -> None:
        """Descarta la sesión y los clientes cacheados. Se usa tras un
        `aws sso login` en otra terminal para forzar la relectura del cache."""
        self._session = None
        self._clients.clear()

    # -- Validación / Capa 3 ---------------------------------------------------
    def _is_session_valid(self) -> bool:
        """True si una llamada real a AWS funciona. Esto dispara — de forma
        transparente — las Capas 1 y 2 de botocore (refresh de rol y relectura
        del token SSO del cache). Si el token SSO ya expiró, lanza excepción."""
        try:
            self.get_client("sts").get_caller_identity()
            return True
        except Exception:  # noqa: BLE001 — cualquier fallo = sesión no válida
            return False

    def ensure_valid_session(
        self,
        max_wait_minutes: int = 10,
        poll_interval_seconds: int = 30,
    ) -> bool:
        """Garantiza una sesión utilizable antes de una tarea larga.

        Retorna True si la sesión es válida (ya lo era, o el usuario renovó
        dentro de la ventana de espera). Retorna False si tras `max_wait_minutes`
        el token SSO sigue expirado. Nunca lanza excepción: no crashea el agente.
        """
        if self._is_session_valid():
            return True

        # Capa 3 — pausa elegante.
        console.print(
            "\n[bold yellow]⚠  Sesión AWS SSO expirada.[/bold yellow]\n"
            "Abre otra terminal y ejecuta:\n"
            f"    [cyan]aws sso login --profile {self.profile_name}[/cyan]\n"
            f"Esperaré hasta {max_wait_minutes} min "
            f"(revisando cada {poll_interval_seconds}s)…"
        )

        deadline = time.monotonic() + max_wait_minutes * 60
        while time.monotonic() < deadline:
            time.sleep(poll_interval_seconds)
            # Relee el cache SSO desde cero (por si acabas de hacer login).
            self._reset()
            if self._is_session_valid():
                console.print("[bold green]✓ Sesión renovada. Retomando.[/bold green]\n")
                return True
            restante = int((deadline - time.monotonic()) / 60)
            console.print(f"  [dim]…sigo esperando (~{max(restante, 0)} min restantes)[/dim]")

        console.print(
            "\n[bold red]✗ No se renovó la sesión a tiempo.[/bold red]\n"
            f"Cuando puedas, ejecuta: [cyan]aws sso login --profile {self.profile_name}[/cyan]\n"
            "y vuelve a lanzar la tarea.\n"
        )
        return False

    # -- Estado del token SSO --------------------------------------------------
    def sso_token_status(self) -> dict[str, Any]:
        """Estado del token SSO y datos del perfil, para diagnóstico/README.

        Devuelve un dict con:
            valid            : bool  — hay un token no expirado en el cache.
            minutes_remaining: int | None — minutos hasta expirar.
            expires_at       : str | None — ISO 8601 de expiración.
            sso_account_id   : str | None — cuenta detectada (del perfil).
            sso_role_name    : str | None — rol detectado (del perfil).
            sso_start_url     : str | None — start URL del perfil.
        """
        account_id, role_name, start_url = self._profile_sso_fields()
        expires_at, minutes = self._cached_token_expiry(start_url)

        return {
            "valid": minutes is not None and minutes > 0,
            "minutes_remaining": minutes,
            "expires_at": expires_at,
            "sso_account_id": account_id,
            "sso_role_name": role_name,
            "sso_start_url": start_url,
        }

    def _profile_sso_fields(self) -> tuple[Optional[str], Optional[str], Optional[str]]:
        """Lee sso_account_id / sso_role_name / start_url del perfil en
        ~/.aws/config. Soporta tanto el formato clásico (claves sso_* en el
        perfil) como el nuevo (sección [sso-session] referenciada)."""
        try:
            botocore_session = self.session._session  # botocore.session.Session
            profile = botocore_session.get_scoped_config()
        except Exception:  # noqa: BLE001 — perfil ausente o mal configurado
            return None, None, None

        account_id = profile.get("sso_account_id")
        role_name = profile.get("sso_role_name")
        start_url = profile.get("sso_start_url")

        # Formato nuevo: el start_url vive en [sso-session <nombre>].
        if start_url is None and profile.get("sso_session"):
            try:
                full = botocore_session.full_config
                sso_sessions = full.get("sso_sessions", {})
                sess = sso_sessions.get(profile["sso_session"], {})
                start_url = sess.get("sso_start_url")
            except Exception:  # noqa: BLE001
                pass

        return account_id, role_name, start_url

    def _cached_token_expiry(self, start_url: Optional[str]) -> tuple[Optional[str], Optional[int]]:
        """Busca en ~/.aws/sso/cache/*.json el token con accessToken vigente.
        Prefiere el que coincide con `start_url`. Devuelve (expires_at_iso,
        minutos_restantes) o (None, None) si no hay token válido."""
        if not _SSO_CACHE_DIR.is_dir():
            return None, None

        best: tuple[Optional[str], Optional[int]] = (None, None)
        for cache_file in _SSO_CACHE_DIR.glob("*.json"):
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            if "accessToken" not in data or "expiresAt" not in data:
                continue

            expires_at = self._parse_expiry(data["expiresAt"])
            if expires_at is None:
                continue

            minutes = int((expires_at - datetime.now(timezone.utc)).total_seconds() / 60)
            iso = expires_at.isoformat()

            # Coincidencia exacta con el start_url del perfil → resultado final.
            if start_url and data.get("startUrl") == start_url:
                return iso, minutes

            # Si no, nos quedamos con el token de mayor vigencia como fallback.
            if best[1] is None or minutes > best[1]:
                best = (iso, minutes)

        return best

    @staticmethod
    def _parse_expiry(raw: str) -> Optional[datetime]:
        """Parsea el campo expiresAt del cache SSO. Formatos vistos:
        '2026-07-14T20:00:00Z' y '2026-07-14T20:00:00UTC'."""
        candidate = raw.strip()
        try:
            # ISO 8601 con 'Z'.
            return datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            # Formato legacy con sufijo 'UTC'.
            naive = datetime.strptime(candidate, "%Y-%m-%dT%H:%M:%SUTC")
            return naive.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
