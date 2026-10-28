"""Reglas de riesgo del core: mensaje genérico sin config y RISK_PATTERNS del proyecto demo."""

import hashlib
import pathlib

from minka.config_loader import config
from minka.core.tools.analysis import risk_analyzer as ra
from minka.core.tools.git import pr_tools as pt

FIX = pathlib.Path(__file__).parent / "fixtures" / "demo"
SHA = {
    "risk_detect.txt": (
        "adb83c54e52d119fea7ad2c503a104fe0d72b776f1427389cd51263c42d42671"
    ),
    "risk_pci.txt": (
        "521f9c812a8c8dd1b1bc71e0cb0d184816f8e69f68219443dad91aa8642946c0"
    ),
    "pr_validate_diff.txt": (
        "22f7a8aeed45e8b163b2462c04a0b0f142ebff5e05dc359dc9c813e8bfcbcb94"
    ),
}
# Mismo input con el que se generaron las fixtures del demo.
GO = (
    'package main\n\nfunc f() {\n\ttoken := "0123456789ABCDEF"\n\tpanic("x")\n'
    '\t// FIXME-DEMO revisar\n\tk := "-----BEGIN RSA PRIVATE KEY-----"\n}\n'
)
DIFF = '+token := "0123456789ABCDEF"\n+panic("x")\n+// FIXME-DEMO revisar\n'


def _fixture(name: str) -> str:
    raw = (FIX / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == SHA[name]
    return raw.decode("utf-8")


def render_risks(root: pathlib.Path) -> tuple[str, str]:
    (root / "svc.go").write_text(GO)
    risks = ra.detect_risks(str(root)).replace(str(root), "<ROOT>") + "\n"
    pci = ra.check_pci_compliance(str(root)).replace(str(root), "<ROOT>") + "\n"
    return risks, pci


def render_diff_alerts(mp) -> str:
    mp.setattr(pt, "show_diff", lambda _p: DIFF)
    return pt.validate_diff("/no/importa") + "\n"


def test_sin_config_detecta_con_mensaje_generico(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RISK_PATTERNS", [])
    monkeypatch.setattr(config, "PR_DIFF_ALERTS", [])
    (tmp_path / "svc.go").write_text('package main\n\nvar token = "0123456789ABCDEF"\n')

    risks = ra.detect_risks(str(tmp_path))
    assert risks.count("HARDCODED_TOKEN") == 1
    assert "Posible token/llave de ejemplo hardcodeado" in risks
    assert "HARDCODED_TOKEN" in ra.check_pci_compliance(str(tmp_path))

    monkeypatch.setattr(pt, "show_diff", lambda _p: '+var token = "0123456789ABCDEF"\n')
    alerts = pt.validate_diff("/no/importa")
    assert alerts == "⚠️ Token/llave de ejemplo hardcodeado detectado."


def test_detect_risks_demo_igual_fixture(tmp_path, demo_global):
    assert render_risks(tmp_path)[0] == _fixture("risk_detect.txt")


def test_pci_demo_igual_fixture(tmp_path, demo_global):
    assert render_risks(tmp_path)[1] == _fixture("risk_pci.txt")


def test_validate_diff_demo_igual_fixture(monkeypatch, demo_global):
    assert render_diff_alerts(monkeypatch) == _fixture("pr_validate_diff.txt")
