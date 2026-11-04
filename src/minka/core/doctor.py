"""doctor — diagnóstico local del entorno (`minka doctor`), sin red ni modelo.

Cada chequeo devuelve ✓/✗, un detalle sin valores sensibles (nunca rutas de repos,
tokens ni claves) y qué hacer. `critical=True` marca los que impiden trabajar: con
alguno en ✗ el comando sale con código 1.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MIN_PYTHON = (3, 12)


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    hint: str = ""
    critical: bool = True
    optional: bool = False  # "–": falta algo que no hace falta en esta configuración

    @property
    def mark(self) -> str:
        if self.optional:
            return "–"
        return "✓" if self.ok else "✗"


def sso_token_minutes(cache_dir: Path | None = None, now: datetime | None = None) -> int | None:
    """Minutos restantes del token SSO más reciente en ~/.aws/sso/cache (solo lectura local).
    None si no hay token."""
    cache_dir = cache_dir or Path.home() / ".aws" / "sso" / "cache"
    now = now or datetime.now(timezone.utc)
    best: datetime | None = None
    if not cache_dir.is_dir():
        return None
    for path in cache_dir.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if "accessToken" not in data or not data.get("expiresAt"):
            continue
        try:
            exp = datetime.fromisoformat(str(data["expiresAt"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=timezone.utc)
        best = exp if best is None or exp > best else best
    if best is None:
        return None
    return int((best - now).total_seconds() // 60)


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def run_checks(cfg, *, sso_cache_dir: Path | None = None, now: datetime | None = None) -> list[Check]:
    checks: list[Check] = []

    v = sys.version_info
    checks.append(
        Check(
            "Python",
            (v.major, v.minor) >= MIN_PYTHON,
            f"{v.major}.{v.minor}.{v.micro}",
            f"Se requiere Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} o superior.",
        )
    )

    checks.append(
        Check(
            "uv",
            shutil.which("uv") is not None,
            "en PATH" if shutil.which("uv") else "no encontrado",
            "Instala uv (https://docs.astral.sh/uv/) para instalar y actualizar el agente.",
            critical=False,
        )
    )

    data_dir = Path(getattr(cfg, "DATA_DIR", "."))
    local = data_dir / "config_local.py"
    env = data_dir / ".env"
    # config_local.py es imprescindible (REPOS, DEFAULT_PROJECT). .env es opcional cuando
    # config_local.py ya define el backend (LLM_BACKEND o AWS_PROFILE): entonces su
    # ausencia se marca "–", no ✗.
    missing_files = [n for n, p in (("config_local.py", local), (".env", env)) if not p.is_file()]
    backend_in_local = bool(
        local.is_file()
        and re.search(r"^\s*(LLM_BACKEND|AWS_PROFILE)\s*=", local.read_text(encoding="utf-8", errors="ignore"), re.M)
    )
    env_optional = missing_files == [".env"] and backend_in_local
    checks.append(
        Check(
            "Directorio de datos",
            not missing_files or env_optional,
            "config_local.py y .env presentes"
            if not missing_files
            else ("sin .env (opcional: backend definido en config_local.py)" if env_optional else "falta " + " y ".join(missing_files)),
            "Corre `minka init` para crearlos sin sobrescribir.",
            critical=not local.is_file(),
            optional=env_optional,
        )
    )

    use_bedrock = bool(getattr(cfg, "USE_BEDROCK", False))
    if use_bedrock:
        minutes = sso_token_minutes(sso_cache_dir, now)
        ok = minutes is not None and minutes > 0
        detail = f"token SSO vigente ({minutes} min)" if ok else "token SSO vencido o ausente"
        profile = getattr(cfg, "AWS_PROFILE", "bedrock")
        checks.append(Check("Sesión AWS", ok, detail, f"Ejecuta `aws sso login --profile {profile}`."))
    else:
        has_key = bool(os.getenv("ANTHROPIC_API_KEY"))
        checks.append(
            Check(
                "API key",
                has_key,
                "ANTHROPIC_API_KEY definida" if has_key else "ANTHROPIC_API_KEY ausente",
                "Defínela en <DATA_DIR>/.env o exporta LLM_BACKEND=bedrock con un perfil SSO.",
            )
        )

    repos = {k: v for k, v in (getattr(cfg, "REPOS", {}) or {}).items() if v}
    missing = [alias for alias, path in repos.items() if not Path(str(path)).expanduser().is_dir()]
    source = getattr(cfg, "REPOS_SOURCE", "none")
    if not repos:
        checks.append(
            Check(
                "Repos",
                False,
                "REPOS vacío",
                "Declara {alias: ruta relativa} en projects/<p>/config.py → REPOS y REPOS_ROOT en config_local.py.",
            )
        )
    else:
        checks.append(
            Check(
                "Repos",
                not missing,
                f"{len(repos) - len(missing)}/{len(repos)} rutas existen"
                + (f"; faltan: {', '.join(missing)}" if missing else ""),
                "Revisa REPOS_ROOT / REPOS_OVERRIDES en config_local.py y las rutas relativas del proyecto.",
            )
        )
        if source == "local":
            checks.append(
                Check(
                    "REPOS por proyecto",
                    False,
                    "REPOS sigue en config_local.py (esquema viejo, deprecado)",
                    "Mueve los alias a projects/<p>/config.py → REPOS (rutas relativas) y deja REPOS_ROOT en config_local.py.",
                    critical=False,
                )
            )

    rag = bool(getattr(cfg, "RAG_ENABLED", False))
    chroma = _module_available("chromadb")
    checks.append(
        Check(
            "chromadb",
            chroma or not rag,
            "instalado" if chroma else ("ausente (RAG_ENABLED=True)" if rag else "ausente (RAG apagado)"),
            "Instala el extra: `uv tool install 'minka-workspace[rag] @ git+<url>'`.",
            critical=rag,
        )
    )

    project = getattr(cfg, "ACTIVE_PROJECT", None)
    projects_dir = Path(getattr(cfg, "PROJECTS_DIR", data_dir / "projects"))
    project_ok = bool(project) and (projects_dir / project).is_dir()
    origin = getattr(cfg, "PROJECT_ORIGIN", None)
    checks.append(
        Check(
            "Proyecto activo",
            project_ok,
            (f"{project} (por {origin})" if origin else project)
            if project_ok
            else ("sin DEFAULT_PROJECT ni repo del cwd" if not project else f"{project}: carpeta ausente"),
            "Define DEFAULT_PROJECT en config_local.py o crea el proyecto con `minka --new-project <nombre>`.",
        )
    )

    from minka.core.memory.live_context import active_context_dir

    ac_dir = active_context_dir(cfg)
    ac_file = ac_dir / f"{project}.md" if project else None
    ac_ok = ac_dir.is_dir() and bool(ac_file and ac_file.is_file())
    checks.append(
        Check(
            "Contexto vivo",
            ac_ok,
            "active-context/<proyecto>.md presente" if ac_ok else ("carpeta ausente" if not ac_dir.is_dir() else "<proyecto>.md ausente"),
            "Ajusta ACTIVE_CONTEXT_DIR en config_local.py o crea <proyecto>.md en esa carpeta.",
            critical=False,
        )
    )
    return checks


def has_critical_failure(checks: list[Check]) -> bool:
    return any(c.critical and not c.ok for c in checks)


def format_report(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{c.mark} {c.name.ljust(width)}  {c.detail}" + (f"\n    → {c.hint}" if not c.ok else "") for c in checks]
    bad = [c for c in checks if not c.ok]
    lines.append("")
    lines.append(
        "Todo en orden." if not bad else f"{len(bad)} chequeo(s) con problemas"
        + (" (hay bloqueantes)." if has_critical_failure(checks) else " (ninguno bloqueante).")
    )
    return "\n".join(lines)
