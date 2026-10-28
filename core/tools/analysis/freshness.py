"""
freshness.py — Detecta qué .md del knowledge tienen fuentes de código que cambiaron.

Problema: los .md de knowledge/extracted/ se derivan del código real, pero se
desactualizan en silencio cuando los repos avanzan. Este módulo compara, por
documento, el commit en que se extrajo contra el HEAD actual de cada repo
fuente, y lista los archivos que cambiaron dentro de las rutas declaradas.

Manifiesto por proyecto: projects/<proyecto>/knowledge/extracted/_sources.json
{
  "schema_version": 1,
  "docs": {
    "extracted/architecture.md": {
      "extracted_at": "2026-08-18",
      "sources": {
        "pos": {"commit": "1efc9105", "paths": ["cmd/", "gateway/", "serverless.yml"]},
        "hsm": {"commit": "d629007",  "paths": ["service/", "serverless.yml"]}
      }
    }
  }
}
Las claves de `sources` son las de config.REPOS. `paths` son pathspecs de git
relativos al repo; vendor/ se excluye siempre.

Solo lectura: no escribe en ChromaDB ni en los .md. Cada llamada a git es
best-effort: si un commit no existe en el checkout local, el documento queda
en estado "unknown" con la razón, nunca se lanza.

Uso (CLI, desde dev-agent/):
    python -m core.tools.analysis.freshness --project <proyecto> [--json]
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

MANIFEST_REL = Path("knowledge") / "extracted" / "_sources.json"
SCHEMA_VERSION = 1

STATUS_FRESH = "fresh"  # ninguna fuente cambió desde la extracción
STATUS_STALE = "stale"  # al menos una fuente cambió
STATUS_UNKNOWN = "unknown"  # no se pudo comparar (commit ausente, repo sin ruta…)

_GIT_TIMEOUT_S = 15


@dataclass
class DocFreshness:
    doc: str
    status: str
    extracted_at: str
    changed: dict[str, list[str]] = field(default_factory=dict)  # repo -> archivos
    heads: dict[str, str] = field(default_factory=dict)  # repo -> HEAD actual
    notes: list[str] = field(default_factory=list)

    @property
    def changed_count(self) -> int:
        return sum(len(v) for v in self.changed.values())


# ---------------------------------------------------------------------------
# git (best-effort)
# ---------------------------------------------------------------------------


def _git(repo_root: Path, *args: str) -> tuple[int, str]:
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
        )
        return res.returncode, (res.stdout if res.returncode == 0 else res.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def git_head(repo_root: Path) -> str:
    code, out = _git(repo_root, "rev-parse", "--short=12", "HEAD")
    return out if code == 0 else ""


def changed_files(repo_root: Path, commit: str, paths: list[str]) -> tuple[list[str] | None, str]:
    """Archivos cambiados entre `commit` y HEAD dentro de `paths` (vendor/ excluido).
    Retorna (lista, "") o (None, razón) si no se pudo comparar."""
    if not repo_root.is_dir():
        return None, f"ruta del repo no existe: {repo_root}"
    code, _ = _git(repo_root, "cat-file", "-e", f"{commit}^{{commit}}")
    if code != 0:
        return None, f"commit {commit} no está en el checkout local (haz fetch/pull)"
    pathspecs = [*paths, ":(exclude)vendor/"]
    code, out = _git(repo_root, "diff", "--name-only", commit, "HEAD", "--", *pathspecs)
    if code != 0:
        return None, f"git diff falló: {out}"
    return [ln for ln in out.splitlines() if ln.strip()], ""


# ---------------------------------------------------------------------------
# manifiesto y chequeo
# ---------------------------------------------------------------------------


def manifest_path(config, project: str) -> Path:
    return Path(config.PROJECTS_DIR) / project / MANIFEST_REL


def load_manifest(path: Path) -> dict:
    """Lee el manifiesto. Sin archivo → {} (proyecto sin docs derivados declarados)."""
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    version = data.get("schema_version")
    if version != SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {version} no soportada (esperada {SCHEMA_VERSION})"
        )
    return data.get("docs", {})


def check_freshness(
    config, project: str | None = None, manifest: Path | None = None
) -> list[DocFreshness]:
    """Evalúa cada documento del manifiesto contra el HEAD actual de sus repos.
    `project` default: config.ACTIVE_PROJECT. `manifest` permite una ruta explícita
    (tests / dry-run). Nunca lanza por errores de git; sí por manifiesto inválido."""
    project = project or getattr(config, "ACTIVE_PROJECT", None)
    if not project and manifest is None:
        return []
    path = manifest or manifest_path(config, project)
    docs = load_manifest(path)
    repos: dict[str, str] = getattr(config, "REPOS", {}) or {}

    results: list[DocFreshness] = []
    for doc, spec in docs.items():
        result = DocFreshness(
            doc=doc, status=STATUS_FRESH, extracted_at=spec.get("extracted_at", "?")
        )
        for repo, src in spec.get("sources", {}).items():
            repo_path = repos.get(repo)
            if not repo_path:
                result.notes.append(f"{repo}: no está en config.REPOS")
                result.status = STATUS_UNKNOWN
                continue
            root = Path(repo_path)
            result.heads[repo] = git_head(root)
            files, reason = changed_files(root, src["commit"], src.get("paths") or ["."])
            if files is None:
                result.notes.append(f"{repo}: {reason}")
                if result.status != STATUS_STALE:
                    result.status = STATUS_UNKNOWN
                continue
            if files:
                result.changed[repo] = files
                result.status = STATUS_STALE
        results.append(result)
    return results


def stale_docs(results: list[DocFreshness]) -> dict[str, DocFreshness]:
    """{doc: resultado} solo de los documentos con fuentes cambiadas."""
    return {r.doc: r for r in results if r.status == STATUS_STALE}


def format_report(results: list[DocFreshness], max_files: int = 8) -> str:
    """Reporte legible para el dev (Claude Code lo muestra al cargar contexto)."""
    if not results:
        return "Sin manifiesto de fuentes: ningún documento derivado declarado."
    icon = {STATUS_FRESH: "✅", STATUS_STALE: "⚠️ ", STATUS_UNKNOWN: "❓"}
    lines = []
    n_stale = sum(1 for r in results if r.status == STATUS_STALE)
    lines.append(
        f"Frescura del knowledge: {n_stale} de {len(results)} documentos con fuentes cambiadas."
    )
    for r in results:
        lines.append(f"{icon[r.status]} {r.doc} (extraído {r.extracted_at})")
        for repo, files in r.changed.items():
            shown = ", ".join(files[:max_files])
            extra = f" … +{len(files) - max_files}" if len(files) > max_files else ""
            lines.append(
                f"    {repo} @{r.heads.get(repo, '?')}: {len(files)} archivo(s): {shown}{extra}"
            )
        for note in r.notes:
            lines.append(f"    · {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI (solo lectura)
# ---------------------------------------------------------------------------


def _main() -> int:
    parser = argparse.ArgumentParser(description="Frescura de los .md derivados del código.")
    parser.add_argument("--project", help="Proyecto (default: DEFAULT_PROJECT de config_local).")
    parser.add_argument("--manifest", type=Path, help="Ruta explícita al _sources.json (dry-run).")
    parser.add_argument("--json", action="store_true", help="Salida JSON en vez de tabla.")
    args = parser.parse_args()

    from config_loader import load_config

    config = load_config(args.project)
    results = check_freshness(config, args.project, args.manifest)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2, ensure_ascii=False))
    else:
        print(format_report(results))
    return 1 if stale_docs(results) else 0


if __name__ == "__main__":
    raise SystemExit(_main())
