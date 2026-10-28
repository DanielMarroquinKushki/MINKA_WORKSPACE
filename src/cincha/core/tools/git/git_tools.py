"""
git_tools — operaciones git sobre los repos Go.

Reglas del proyecto:
- Los commits los hace el DEV, no el agente, salvo Nivel 5+ con AUTO_COMMIT.
- Toda escritura crea rama primero (config.AUTO_BRANCH).
- Nunca se opera sobre main/master directamente.

Se usa subprocess con `git -C <repo>` para apuntar al repo correcto sin cambiar
el cwd del proceso.
"""

import subprocess
from pathlib import Path


def _run_git(repo_path: str, args: list[str], timeout: int = 30) -> tuple[int, str, str]:
    """Ejecuta `git -C repo_path <args>`. Devuelve (returncode, stdout, stderr)."""
    if not Path(repo_path).is_dir():
        return 1, "", f"no existe el repo {repo_path}"
    try:
        proc = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _slugify(reason: str, max_len: int = 40) -> str:
    """Convierte una razón libre en un slug de rama seguro."""
    slug = "".join(c if c.isalnum() else "-" for c in reason.lower())
    slug = "-".join(filter(None, slug.split("-")))
    return slug[:max_len].strip("-") or "change"


def show_diff(repo_path: str, staged: bool = False) -> str:
    """Muestra el diff de trabajo (o del staging si staged=True)."""
    args = ["diff"] + (["--staged"] if staged else [])
    code, out, err = _run_git(repo_path, args)
    if code != 0:
        return f"ERROR git diff: {err}"
    return out or "[sin cambios]"


def git_log(repo_path: str, n: int = 10) -> str:
    """Últimos `n` commits en formato compacto."""
    code, out, err = _run_git(repo_path, ["log", f"-{n}", "--oneline"])
    if code != 0:
        return f"ERROR git log: {err}"
    return out or "[sin commits]"


def current_branch(repo_path: str) -> str:
    """Rama actual del repo."""
    code, out, err = _run_git(repo_path, ["rev-parse", "--abbrev-ref", "HEAD"])
    return out if code == 0 else f"ERROR: {err}"


def create_branch(repo_path: str, reason: str, prefix: str = "agent/") -> str:
    """
    Crea y cambia a una rama nueva `<prefix><slug-de-reason>`.
    Nunca opera si ya estás en la rama destino.
    """
    branch = f"{prefix}{_slugify(reason)}"
    code, out, err = _run_git(repo_path, ["checkout", "-b", branch])
    if code != 0:
        # Puede que la rama ya exista; intenta cambiar a ella.
        code2, _, err2 = _run_git(repo_path, ["checkout", branch])
        if code2 != 0:
            return f"ERROR creando rama {branch}: {err or err2}"
    return branch


def commit_change(repo_path: str, message: str, paths: list[str] | None = None) -> str:
    """
    Hace stage + commit. Solo debe invocarse en Nivel 5+ (AUTO_COMMIT) o si el
    dev lo pide explícitamente. En Nivel 3 el commit lo hace el dev.
    """
    add_args = ["add"] + (paths if paths else ["-A"])
    code, _, err = _run_git(repo_path, add_args)
    if code != 0:
        return f"ERROR git add: {err}"

    code, out, err = _run_git(repo_path, ["commit", "-m", message])
    if code != 0:
        return f"ERROR git commit: {err or out}"
    return f"OK commit: {out}"
