"""extractor — parte determinista de `minka knowledge init` (diseño: docs/diseño-ciclo5-knowledge-init.md).

Por cada repo de config.REPOS y según el STACK del proyecto (python | go):
  árbol a profundidad 3 con conteo por extensión · módulos (carpetas con código) y sus imports
  internos (python: ast; go: imports del módulo declarado en go.mod) · README y manifiesto ·
  `git log -n 20`. Cada afirmación lleva `alias/ruta:línea` verificada con RepoIndex; si no se
  puede verificar, queda marcada [verificar]. Solo hechos citables: nada de estados ni juicios.

Seguridad, ANTES de escribir nada: archivos y carpetas sensibles nunca se leen
(EXCLUDED_NAMES / EXCLUDED_DIRS, binarios, > MAX_FILE_BYTES) y todo lo leído pasa por un
escaneo de patrones de secretos que aborta listando `ruta:línea (patrón)`, nunca el valor.

$0: sin modelo. Node no está soportado todavía (UnsupportedStack).
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from minka.core import stacks
from minka.core.tools.analysis.citations import STATUS_OK, RepoIndex

MAX_FILE_BYTES = 1_000_000
TREE_DEPTH = 3
GIT_LOG_N = 20
README_LINES = 60
VERIFY = "[verificar]"

EXCLUDED_NAMES = (
    ".env*", "*.pem", "*.key", "*.p12", "*.jks", "id_rsa*", "credentials", "secrets*",
)
EXCLUDED_DIRS = frozenset(
    {".aws", "vendor", "node_modules", ".venv", "venv", "dist", "build", ".git", "__pycache__", "mocks"}
)

# (id, regex). Formato compatible con RISK_PATTERNS del proyecto (se suman por id).
SECRET_PATTERNS: list[tuple[str, str]] = [
    ("AWS_ACCESS_KEY", r"AKIA[0-9A-Z]{16}"),
    ("PRIVATE_KEY", r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY"),
    ("JWT", r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
    ("API_KEY_SK", r"\bsk-[A-Za-z0-9_-]{20,}"),
    ("LONG_HEX", r"\b[A-Fa-f0-9]{40,}\b"),
    ("LONG_BASE64", r"[A-Za-z0-9+/]{40,}={0,2}"),
    ("CREDENTIAL_ASSIGN", r"(?i)\b(password|passwd|secret|token|api_key)\s*[:=]\s*[\"'][^\"']{8,}[\"']"),
]
_SECRET_RES = [(sid, re.compile(rx)) for sid, rx in SECRET_PATTERNS]
_INTERESTING_EXT = (".md", ".yml", ".yaml", ".json", ".toml", ".mod", ".txt")
_NO_JUDGEMENT_WORDS = ("vigente", "cerrado", "mitigado", "riesgo", "deuda", "bug", "crítico")


class UnsupportedStack(Exception):
    pass


class SecretFound(Exception):
    def __init__(self, hits: list[str]):
        super().__init__(f"{len(hits)} posible(s) secreto(s) en lo leído; abortado sin escribir.")
        self.hits = hits


@dataclass
class ModuleFacts:
    path: str  # relativo al repo ("." = raíz)
    files: int = 0
    imports: list[tuple[str, str]] = field(default_factory=list)  # (destino, cita)


@dataclass
class RepoFacts:
    alias: str
    root: Path
    stack: str
    commit: str
    tree: list[str] = field(default_factory=list)
    ext_counts: Counter = field(default_factory=Counter)
    modules: dict[str, ModuleFacts] = field(default_factory=dict)
    readme: str | None = None  # cita del README
    manifest: str | None = None  # cita del manifiesto
    module_name: str | None = None  # go: módulo del go.mod
    git_log: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    files_read: int = 0


# ---------------------------------------------------------------------------
# seguridad
# ---------------------------------------------------------------------------


def is_excluded(path: Path, root: Path) -> bool:
    rel_parts = path.relative_to(root).parts
    if any(part in EXCLUDED_DIRS for part in rel_parts[:-1]):
        return True
    name = rel_parts[-1]
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDED_NAMES)


def safe_read(path: Path, root: Path) -> str | None:
    """Texto del archivo o None si está excluido, es binario o demasiado grande."""
    if is_excluded(path, root):
        return None
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:4096]:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def scan_secrets(text: str, label: str, extra: list | None = None) -> list[str]:
    """`label:línea (ID)` por cada patrón de secreto; nunca devuelve el valor."""
    patterns = list(_SECRET_RES)
    for rule in extra or []:
        try:
            patterns.append((rule[0], re.compile(rule[2])))
        except (re.error, IndexError, TypeError):
            continue
    hits: list[str] = []
    for n, line in enumerate(text.splitlines(), 1):
        for sid, rx in patterns:
            if rx.search(line):
                hits.append(f"{label}:{n} ({sid})")
                break
    return hits


# ---------------------------------------------------------------------------
# hechos por repo
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    try:
        res = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15)
        return res.stdout.strip() if res.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _cite(index: RepoIndex, alias: str, rel: str, line: int | None) -> str:
    """`alias/rel:línea` si RepoIndex lo verifica (archivo y línea), si no `… [verificar]`."""
    raw = f"{alias}/{rel}" + (f":{line}" if line else "")
    status, matches = index.resolve(f"{alias}/{rel}")
    if status != STATUS_OK:
        return f"{raw} {VERIFY}"
    if line:
        _, real = matches[0]
        try:
            total = len(real.read_text(encoding="utf-8", errors="ignore").splitlines())
        except OSError:
            return f"{raw} {VERIFY}"
        if line > total:
            return f"{raw} {VERIFY}"
    return raw


def _tree(root: Path, profile: stacks.StackProfile) -> tuple[list[str], Counter]:
    lines: list[str] = []
    counts: Counter = Counter()
    code_ext = set(profile.extensions)

    def walk(d: Path, depth: int) -> None:
        try:
            entries = sorted(d.iterdir(), key=lambda p: (not p.is_dir(), p.name))
        except OSError:
            return
        local: Counter = Counter()
        for e in entries:
            if e.is_dir():
                if e.name in EXCLUDED_DIRS or e.name.startswith("."):
                    continue
                if depth < TREE_DEPTH:
                    walk(e, depth + 1)
            elif e.is_file() and not is_excluded(e, root):
                ext = e.suffix.lower() or "(sin ext)"
                if ext in code_ext or ext in _INTERESTING_EXT:
                    local[ext] += 1
                    counts[ext] += 1
        if local:
            rel = d.relative_to(root).as_posix() or "."
            summary = ", ".join(f"{k} ×{v}" for k, v in sorted(local.items()))
            lines.append(f"{'  ' * depth}{rel}/ — {summary}")

    walk(root, 0)
    return lines, counts


def _python_imports(root: Path, alias: str, index: RepoIndex, facts: RepoFacts, cfg=None) -> None:
    files = list(stacks.code_files(root, cfg, extra_skip=tuple(EXCLUDED_DIRS)))
    top_level = {p.relative_to(root).parts[0].removesuffix(".py") for p in files if p.relative_to(root).parts}
    for f in files:
        text = safe_read(f, root)
        if text is None:
            continue
        facts.files_read += 1
        rel = f.relative_to(root).as_posix()
        mod_dir = f.parent.relative_to(root).as_posix() or "."
        mod = facts.modules.setdefault(mod_dir, ModuleFacts(path=mod_dir))
        mod.files += 1
        try:
            tree = ast.parse(text)
        except SyntaxError:
            facts.notes.append(f"{alias}/{rel}: no se pudo parsear (SyntaxError)")
            continue
        for node in ast.walk(tree):
            targets: list[str] = []
            if isinstance(node, ast.Import):
                targets = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                targets = [node.module]
            elif isinstance(node, ast.ImportFrom) and node.level > 0:
                targets = [f"{'.' * node.level}{node.module or ''}"]
            for t in targets:
                head = t.split(".")[0]
                if t.startswith(".") or head in top_level:
                    mod.imports.append((t, _cite(index, alias, rel, getattr(node, "lineno", None))))


_GO_IMPORT_RE = re.compile(r'^\s*(?:import\s+)?(?:\w+\s+)?"([^"]+)"')


def _go_imports(root: Path, alias: str, index: RepoIndex, facts: RepoFacts, cfg=None) -> None:
    gomod = root / "go.mod"
    module = None
    text = safe_read(gomod, root) if gomod.is_file() else None
    if text:
        m = re.search(r"^module\s+(\S+)", text, re.M)
        module = m.group(1) if m else None
        facts.files_read += 1
    facts.module_name = module
    if not module:
        facts.notes.append(f"{alias}: sin go.mod legible; los imports internos quedan {VERIFY}")
    for f in stacks.code_files(root, cfg, extra_skip=tuple(EXCLUDED_DIRS)):
        src = safe_read(f, root)
        if src is None:
            continue
        facts.files_read += 1
        rel = f.relative_to(root).as_posix()
        mod_dir = f.parent.relative_to(root).as_posix() or "."
        mod = facts.modules.setdefault(mod_dir, ModuleFacts(path=mod_dir))
        mod.files += 1
        in_block = False
        for n, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith("import ("):
                in_block = True
                continue
            if in_block and s.startswith(")"):
                in_block = False
                continue
            if not (in_block or s.startswith("import ")):
                continue
            m = _GO_IMPORT_RE.match(line)
            if not m:
                continue
            target = m.group(1)
            if module and target.startswith(module + "/"):
                mod.imports.append((target[len(module) + 1 :], _cite(index, alias, rel, n)))
            elif not module and "/" in target:
                # Sin go.mod no se distingue interno de externo: se lista con [verificar].
                mod.imports.append((target, f"{alias}/{rel}:{n} {VERIFY}"))


def extract_repo(alias: str, root: Path, profile: stacks.StackProfile, index: RepoIndex, cfg=None) -> RepoFacts:
    facts = RepoFacts(alias=alias, root=root, stack=profile.name, commit=_git(root, "rev-parse", "--short=12", "HEAD") or "sin-git")
    facts.tree, facts.ext_counts = _tree(root, profile)
    if profile.name == "python":
        _python_imports(root, alias, index, facts, cfg)
    else:
        _go_imports(root, alias, index, facts, cfg)
    for name in ("README.md", "readme.md", "README.rst", "README"):
        if (root / name).is_file() and safe_read(root / name, root) is not None:
            facts.readme = _cite(index, alias, name, 1)
            facts.files_read += 1
            break
    if (root / profile.deps_file).is_file() and safe_read(root / profile.deps_file, root) is not None:
        facts.manifest = _cite(index, alias, profile.deps_file, 1)
        if profile.name != "go":  # go.mod ya se contó al leer el módulo
            facts.files_read += 1
    log = _git(root, "log", f"-n{GIT_LOG_N}", "--format=%h %cs %s")
    facts.git_log = [ln[:120] for ln in log.splitlines()] if log else []
    return facts


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def _minka_version() -> str:
    try:
        from importlib.metadata import version

        return version("minka-workspace")
    except Exception:  # noqa: BLE001
        return "dev"


def render_architecture(project: str, repos: list[RepoFacts], generated_at: str) -> str:
    head = " ".join(f"{r.alias}@{r.commit}" for r in repos)
    out = [
        f"# Arquitectura — {project} (borrador generado)",
        "",
        f"Generado: {generated_at} · minka {_minka_version()} · repos: {head}",
        "",
        "Hechos extraídos del código con `minka knowledge init`; cada línea cita `alias/archivo:línea`",
        f"verificada o lleva {VERIFY}. Completa la narrativa a mano sin quitar las citas.",
        "",
    ]
    for r in repos:
        out += [f"## Repo `{r.alias}` ({r.stack})", "", f"fuente: {r.alias}@{r.commit}", ""]
        if r.manifest:
            out.append(f"- Manifiesto: {r.manifest}")
        if r.readme:
            out.append(f"- README: {r.readme}")
        if r.module_name:
            out.append(f"- Módulo Go declarado en {r.alias}/go.mod:1")
        total = sum(r.ext_counts.values())
        out.append(f"- Archivos indexables: {total} (" + ", ".join(f"{k} ×{v}" for k, v in sorted(r.ext_counts.items())) + ")")
        out += ["", "### Árbol (profundidad 3)", "", "```", *r.tree, "```", "", "### Módulos e imports internos", ""]
        for mod in sorted(r.modules.values(), key=lambda m: m.path):
            out.append(f"- `{mod.path}/` — {mod.files} archivo(s)")
            seen: set[str] = set()
            for target, cite in mod.imports:
                key = (target, cite)
                if key in seen:
                    continue
                seen.add(key)
                out.append(f"  - importa `{target}` — {cite}")
        if r.git_log:
            out += ["", f"### Actividad reciente (git log, últimos {len(r.git_log)})", ""]
            out += [f"- {ln}" for ln in r.git_log]
        if r.notes:
            out += ["", "### Avisos", ""] + [f"- {n}" for n in r.notes]
        out.append("")
    return "\n".join(out)


def render_sources(repos: list[RepoFacts], date: str) -> dict:
    return {
        "schema_version": 1,
        "docs": {
            "extracted/architecture.md": {
                "extracted_at": date,
                "sources": {
                    r.alias: {"commit": r.commit, "paths": sorted(m.path for m in r.modules.values())}
                    for r in repos
                },
            }
        },
    }


def render_report(project: str, repos: list[RepoFacts], generated_at: str) -> str:
    lines = [f"knowledge init — {project} — {generated_at}", ""]
    for r in repos:
        imports = sum(len(m.imports) for m in r.modules.values())
        unverified = sum(1 for m in r.modules.values() for _, c in m.imports if VERIFY in c)
        lines.append(
            f"{r.alias}: stack={r.stack} commit={r.commit} archivos_leidos={r.files_read} "
            f"modulos={len(r.modules)} imports_internos={imports} sin_verificar={unverified} "
            f"git_log={len(r.git_log)} avisos={len(r.notes)}"
        )
    return "\n".join(lines) + "\n"


def contains_judgement(text: str) -> list[str]:
    """Palabras de estado/juicio que el borrador no debe contener (test de 'solo hechos')."""
    body = text.split("\n", 6)[-1].lower()  # salta el encabezado explicativo
    return [w for w in _NO_JUDGEMENT_WORDS if re.search(rf"\b{w}s?\b", body)]


# ---------------------------------------------------------------------------
# orquestación
# ---------------------------------------------------------------------------


@dataclass
class Draft:
    project: str
    generated_at: str
    repos: list[RepoFacts]
    architecture: str
    sources: dict
    report: str
    files_scanned: int


def build_draft(cfg, project: str, now: datetime | None = None) -> Draft:
    """Extrae, escanea secretos y renderiza. Lanza UnsupportedStack / SecretFound sin escribir."""
    profile = stacks.profile(cfg)
    if profile.name not in ("python", "go"):
        raise UnsupportedStack(
            f"STACK '{profile.name}' no soportado aún en knowledge init (hoy: python, go)."
        )
    repos_cfg = {k: v for k, v in (getattr(cfg, "REPOS", {}) or {}).items() if v}
    index = RepoIndex(repos_cfg)
    now = now or datetime.now(timezone.utc)
    generated_at = now.strftime("%Y-%m-%d %H:%M UTC")
    extra_rules = list(getattr(cfg, "RISK_PATTERNS", []) or [])

    hits: list[str] = []
    repos: list[RepoFacts] = []
    scanned = 0
    for alias, path in repos_cfg.items():
        root = Path(path).expanduser()
        if not root.is_dir():
            continue
        # Escaneo de secretos sobre TODO lo que se leería (código, README, manifiesto).
        for f in list(stacks.code_files(root, cfg, extra_skip=tuple(EXCLUDED_DIRS))) + [
            root / n for n in ("README.md", "readme.md", profile.deps_file) if (root / n).is_file()
        ]:
            text = safe_read(f, root)
            if text is None:
                continue
            scanned += 1
            hits += scan_secrets(text, f"{alias}/{f.relative_to(root).as_posix()}", extra_rules)
        repos.append(extract_repo(alias, root, profile, index, cfg))
    if hits:
        raise SecretFound(hits)

    date = now.strftime("%Y-%m-%d")
    return Draft(
        project=project,
        generated_at=generated_at,
        repos=repos,
        architecture=render_architecture(project, repos, generated_at),
        sources=render_sources(repos, date),
        report=render_report(project, repos, generated_at),
        files_scanned=scanned,
    )


def write_draft(draft: Draft, out_dir: Path) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "architecture.md": out_dir / "architecture.md",
        "_sources.json": out_dir / "_sources.json",
        "report.txt": out_dir / "report.txt",
    }
    paths["architecture.md"].write_text(draft.architecture, encoding="utf-8")
    paths["_sources.json"].write_text(json.dumps(draft.sources, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["report.txt"].write_text(draft.report, encoding="utf-8")
    return paths


def apply_draft(draft: Draft, extracted_dir: Path) -> dict[str, Path]:
    """Copia a knowledge/extracted/: crea si no existe; si existe escribe `.new`. Nunca sobrescribe."""
    extracted_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}
    for name, content in (
        ("architecture.md", draft.architecture),
        ("_sources.json", json.dumps(draft.sources, ensure_ascii=False, indent=2) + "\n"),
    ):
        target = extracted_dir / name
        if target.exists():
            target = extracted_dir / f"{name}.new"
        target.write_text(content, encoding="utf-8")
        written[name] = target
    return written
