"""release.sh / rollback.sh contra un repositorio git local temporal (file://), nunca origin.
Instala con `uv tool` dos veces: tarda ~1 min y necesita las dependencias en caché o red."""

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
