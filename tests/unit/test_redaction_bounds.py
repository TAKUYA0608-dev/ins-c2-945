"""S-3 redaction must not eat digit runs inside amounts while still redacting phone numbers."""

from src.nodes.post_process_node import _redact_report


def test_amounts_survive_redaction():
    report = {
        "detail": "ceded_amount 90000000.0 > gross_amount 80000000.0",
        "gross_amount": 80000000,
        "ceded_amount": 90000000,
    }
    out = _redact_report(report)
    assert out["detail"] == "ceded_amount 90000000.0 > gross_amount 80000000.0"
    assert out["gross_amount"] == 80000000 and out["ceded_amount"] == 90000000


def test_phone_numbers_are_still_redacted():
    out = _redact_report({"note": "call 090-1234-5678 or 03-1234-5678 or +81 3 1234 5678 today"})
    assert "090-1234-5678" not in out["note"] and "03-1234-5678" not in out["note"] and "1234 5678" not in out["note"]
    assert out["note"].count("[REDACTED]") == 3
