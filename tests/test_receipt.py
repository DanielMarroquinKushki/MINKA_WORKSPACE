"""Test offline del recibo del ApprovalGate (best-effort, output/receipts/)."""
from datetime import datetime

from config_loader import config
from core.orchestrator.approval_gate import ApprovalGate


def test_generate_receipt_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(config, "ACTIVE_PROJECT", "amex-colombia")
    monkeypatch.setattr(config, "AUTONOMY_LEVEL", 3)
    monkeypatch.setattr(config, "AUTO_COMMIT", False)

    gate = ApprovalGate(config)
    gate._generate_receipt("pos/keys.go", "fix reversos", "contenido aprobado")

    date = datetime.now().strftime("%Y-%m-%d")
    receipt = tmp_path / "receipts" / f"{date}.md"
    assert receipt.is_file()
    text = receipt.read_text(encoding="utf-8")
    assert "amex-colombia" in text
    assert "pos/keys.go" in text
    assert "Hash (sha256:12):" in text
