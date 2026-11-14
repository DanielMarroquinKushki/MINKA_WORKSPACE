"""release.sh / rollback.sh contra un repositorio git local temporal (file://), nunca origin.
Instala con `uv tool` dos veces: tarda ~1 min y necesita las dependencias en caché o red."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv no está en PATH")
def test_release_y_rollback_contra_repo_local():
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "verify_release_scripts.sh")], capture_output=True, text=True, timeout=900)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert "verificación completa" in proc.stdout
    assert "remoto intacto" in proc.stdout and "MINKA_HOME intacto" in proc.stdout


# ---------------------------------------------------------------------------
# scripts/ci_status.sh con un `gh` falso: el ÚLTIMO run manda; sin runs solo avisa.
# ---------------------------------------------------------------------------

GH_FAKE = """#!/usr/bin/env bash
printf '%s ' "$@" > "$GH_ARGS_FILE"
cat "$GH_OUT_FILE"
exit "${GH_EXIT:-0}"
"""


def _ci_status(tmp_path, output: str, gh_exit: int = 0, sha: str = "a" * 40):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    gh = bin_dir / "gh"
    gh.write_text(GH_FAKE)
    gh.chmod(0o755)
    out_file, args_file = tmp_path / "gh.out", tmp_path / "gh.args"
    out_file.write_text(output)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GH_OUT_FILE": str(out_file),
        "GH_ARGS_FILE": str(args_file),
        "GH_EXIT": str(gh_exit),
    }
    proc = subprocess.run(
        ["bash", str(ROOT / "scripts" / "ci_status.sh"), sha], capture_output=True, text=True, env=env, timeout=30
    )
    args = args_file.read_text() if args_file.exists() else ""
    return proc.returncode, proc.stdout.strip(), args


@pytest.mark.parametrize(
    ("output", "code", "text"),
    [
        ('[{"conclusion":"success","status":"completed"}]', 0, "en verde"),
        ('[{"conclusion":"failure","status":"completed"}]', 1, "terminó en failure"),
        ('[{"conclusion":"cancelled","status":"completed"}]', 1, "terminó en cancelled"),
        ('[{"conclusion":"timed_out","status":"completed"}]', 1, "terminó en timed_out"),
        ("[]", 2, "sin corridas"),
        ('[{"conclusion":"","status":"in_progress"}]', 3, "sin concluir"),
        ('[{"conclusion":"","status":"queued"}]', 3, "sin concluir"),
        ('[{"conclusion":"skipped","status":"completed"}]', 3, "ni éxito ni fallo"),
        # el más reciente va primero: un fallo reciente tapa un éxito anterior y viceversa
        ('[{"conclusion":"failure","status":"completed"},{"conclusion":"success","status":"completed"}]', 1, "failure"),
        ('[{"conclusion":"success","status":"completed"},{"conclusion":"failure","status":"completed"}]', 0, "en verde"),
        # JSON con sangría (gh en terminal)
        ('[\n  {\n    "conclusion": "failure",\n    "status": "completed"\n  }\n]\n', 1, "failure"),
    ],
)
def test_ci_status_decide_por_el_ultimo_run(tmp_path, output, code, text):
    rc, out, args = _ci_status(tmp_path, output)
    assert rc == code, out
    assert text in out and "aaaaaaaaaaaa" in out
    assert "--commit aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" in args and "--json status,conclusion" in args


def test_ci_status_gh_sin_sesion_solo_avisa(tmp_path):
    rc, out, _ = _ci_status(tmp_path, "", gh_exit=1)
    assert rc == 4 and "no pudo consultar" in out


def test_ci_status_sin_gh_en_path_solo_avisa(tmp_path):
    env = {**os.environ, "PATH": str(tmp_path / "vacio")}
    (tmp_path / "vacio").mkdir()
    for tool in ("bash", "sed", "tr", "printf"):
        real = shutil.which(tool)
        if real:
            (tmp_path / "vacio" / tool).symlink_to(real)
    proc = subprocess.run(["bash", str(ROOT / "scripts" / "ci_status.sh"), "b" * 40], capture_output=True, text=True, env=env, timeout=30)
    assert proc.returncode == 4 and "gh no está instalado" in proc.stdout


def test_release_mapea_los_codigos_de_ci_status():
    text = (ROOT / "scripts" / "release.sh").read_text(encoding="utf-8")
    assert 'scripts/ci_status.sh" "$HEAD_SHA"' in text
    assert '1) die "$CI_MSG"' in text and '0) ok "$CI_MSG"' in text and '*) warn "$CI_MSG' in text


def test_tar_portable_opciones_antes_de_operandos():
    text = (ROOT / "scripts" / "verify_release_scripts.sh").read_text(encoding="utf-8")
    assert "tar -c -f - --null -T -" in text and 'tar -x -f - -C "$REPO"' in text
    assert "-cf - -C" not in text  # la forma que GNU tar rechaza ("-C has no effect")
