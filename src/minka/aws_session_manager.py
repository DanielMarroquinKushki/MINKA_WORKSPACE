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

  Capa 3 — Login solo si hace falta  (LO QUE SÍ construimos aquí)
    ensure_sso_login(): si el token del perfil sigue vigente no toca la red.
    Si venció, valida con STS (timeouts cortos; botocore renueva con el
    refreshToken si puede). Solo ante un error de TOKEN lanza
    `aws sso login --profile <perfil>` (con TTY y timeout); sin TTY devuelve
    la instrucción. Un error de red no abre el navegador.

Uso:
    mgr = AWSSessionManager(profile_name="bedrock", region="us-east-1")
    mgr.get_bedrock_client()        # cliente bedrock-runtime listo para usar
    mgr.get_client("s3")            # cualquier cliente AWS
    ok, msg = mgr.ensure_sso_login()  # antes de una tarea: (bool, mensaje)

Seguridad:
    - No hardcodea credenciales, tokens ni secrets.
    - No contiene el sso_start_url (vive en ~/.aws/config del usuario).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
    SSOError,
    SSOTokenLoadError,
    TokenRetrievalError,
    UnauthorizedSSOTokenError,
)
from rich.console import Console

console = Console()

# Directorio donde el AWS CLI cachea el token SSO (accessToken vigente).
_SSO_CACHE_DIR = Path.home() / ".aws" / "sso" / "cache"

# Errores que significan "el token SSO no sirve" (solo estos lanzan el login).
_TOKEN_ERRORS = (SSOTokenLoadError, UnauthorizedSSOTokenError, TokenRetrievalError, SSOError)
_TOKEN_ERROR_CODES = {"UnauthorizedException", "ExpiredToken", "ExpiredTokenException"}
# Errores de red: nunca abren el navegador.
_NETWORK_ERRORS = (
    EndpointConnectionError,
    ConnectTimeoutError,
    ReadTimeoutError,
    ConnectionClosedError,
)


def _classify_error(exc: Exception) -> str:
    """"token", "network" u "other" para una excepción de la validación STS."""
    if isinstance(exc, _NETWORK_ERRORS):
        return "network"
    if isinstance(exc, _TOKEN_ERRORS):
        return "token"
    if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in _TOKEN_ERROR_CODES:
        return "token"
    return "other"


def _is_interactive() -> bool:
    """True si hay una terminal para el login (stdin y stdout son TTY)."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def build_anthropic_bedrock(cfg) -> Any:
    """Cliente AnthropicBedrock con perfil, región y timeouts de `cfg` (único lugar)."""
    import anthropic
    import httpx

    return anthropic.AnthropicBedrock(
        aws_profile=cfg.AWS_PROFILE,
        aws_region=cfg.AWS_REGION,
        timeout=httpx.Timeout(cfg.BEDROCK_TIMEOUT_SECONDS, connect=cfg.BEDROCK_CONNECT_TIMEOUT_SECONDS),
        max_retries=cfg.BEDROCK_MAX_RETRIES,
    )


class AWSSessionManager:
    """Gestiona una sesión AWS basada en un perfil SSO, con renovación
    automática (Capas 1-2 vía botocore) y pausa elegante (Capa 3 propia)."""

    def __init__(
        self,
        profile_name: str = "bedrock",
        region: str = "us-east-1",
        refresh_before_minutes: int = 5,
        login_timeout_seconds: int = 300,
        connect_timeout_seconds: int = 10,
    ) -> None:
        self.profile_name = profile_name
        self.region = region
        # Margen (min) con el que consideramos que un token está "por vencer".
        # botocore renueva solo; esto es informativo para sso_token_status().
        self.refresh_before_minutes = refresh_before_minutes
        self.login_timeout_seconds = login_timeout_seconds
        self.connect_timeout_seconds = connect_timeout_seconds

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
        `aws sso login` para forzar la relectura del cache."""
        self._session = None
        self._clients.clear()

    # -- Validación / Capa 3 ---------------------------------------------------
    def _sts_check(self) -> None:
        """Llamada mínima a STS con timeouts cortos y un solo intento. Lanza la
        excepción de botocore si falla (botocore renueva el token si puede)."""
        cfg = Config(
            connect_timeout=self.connect_timeout_seconds,
            read_timeout=self.connect_timeout_seconds,
            retries={"max_attempts": 1},
        )
        self.session.client("sts", region_name=self.region, config=cfg).get_caller_identity()

    def ensure_sso_login(self) -> tuple[bool, str]:
        """(True, "") si la sesión sirve; (False, mensaje) si no. Nunca lanza.

        Token vigente → sin red. Vencido → STS. Solo un error de token lanza
        `aws sso login` (con TTY); sin TTY o con error de red, devuelve el mensaje.
        """
        status = self.sso_token_status()
        minutes = status.get("minutes_remaining")
        if status.get("valid") and minutes is not None and minutes > self.refresh_before_minutes:
            return True, ""

        login_cmd = f"aws sso login --profile {self.profile_name}"
        try:
            self._sts_check()
            return True, ""
        except Exception as exc:  # noqa: BLE001 — se clasifica abajo
            error = exc

        kind = _classify_error(error)
        if kind == "network":
            return False, (
                f"Sin conexión con AWS ({type(error).__name__}). Revisa la red o la VPN "
                "y reintenta; no se abrió el navegador."
            )
        if kind == "other":
            return False, (
                f"No se pudo validar la sesión AWS del perfil '{self.profile_name}' "
                f"({type(error).__name__}). Revisa ~/.aws/config o corre: minka-sso --verify"
            )
        if not _is_interactive():
            return False, f"Sesión AWS SSO vencida. Renueva con: {login_cmd}"

        console.print(
            f"[bold yellow]Sesión AWS SSO vencida.[/bold yellow] Ejecutando: [cyan]{login_cmd}[/cyan]"
        )
        try:
            subprocess.run(
                ["aws", "sso", "login", "--profile", self.profile_name],
                check=True,
                timeout=self.login_timeout_seconds,
            )
        except FileNotFoundError:
            return False, f"No se encontró la CLI 'aws'. Instálala y ejecuta: {login_cmd}"
        except subprocess.TimeoutExpired:
            return False, (
                f"`{login_cmd}` no terminó en {self.login_timeout_seconds} s. "
                "Reintenta cuando puedas completar el login en el navegador."
            )
        except subprocess.CalledProcessError as exc:
            return False, f"`{login_cmd}` falló (código {exc.returncode})."

        self._reset()
        try:
            self._sts_check()
        except Exception as exc:  # noqa: BLE001
            return False, f"Tras el login la sesión sigue sin validar ({type(exc).__name__})."
        return True, "Sesión AWS renovada."

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
        """Busca en ~/.aws/sso/cache/*.json el token cuyo startUrl es el del perfil.
        Devuelve (expires_at_iso, minutos_restantes) o (None, None) si no hay."""
        if not start_url or not _SSO_CACHE_DIR.is_dir():
            return None, None

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

            # Solo cuenta el token del start_url de ESTE perfil (sin fallback a otros).
            if start_url and data.get("startUrl") == start_url:
                return iso, minutes

        return None, None

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
