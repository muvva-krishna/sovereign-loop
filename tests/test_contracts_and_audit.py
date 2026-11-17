"""Unit tests for cryptographic hash-chaining helpers in contracts/audit_crypto.py."""

from __future__ import annotations

from contracts.audit_crypto import GENESIS_HASH, compute_entry_hash, verify_audit_chain


def _make_row(
    row_id: int,
    prev_hash: str,
    *,
    tenant_id: str = "havells_electricals",
    result: str = "ok",
) -> dict[str, object]:
    ts = f"2026-11-17T10:00:0{row_id}+00:00"
    entry_hash = compute_entry_hash(
        prev_hash=prev_hash,
        tenant_id=tenant_id,
        trace_id=f"trc-{row_id}",
        actor_id="sales-1",
        tool_name="erp.check_inventory",
        arguments_json='{"material_id":"HV-FAN-001"}',
        outcome="ALLOW",
        result_summary=result,
        timestamp_iso=ts,
    )
    return {
        "id": row_id,
        "prev_hash": prev_hash,
        "entry_hash": entry_hash,
        "tenant_id": tenant_id,
        "trace_id": f"trc-{row_id}",
        "actor_id": "sales-1",
        "tool_name": "erp.check_inventory",
        "arguments": '{"material_id":"HV-FAN-001"}',
        "outcome": "ALLOW",
        "result": result,
        "timestamp_iso": ts,
    }


def test_verify_empty_chain() -> None:
    res = verify_audit_chain([])
    assert res.valid is True
    assert res.total_entries == 0
    assert res.checked_entries == 0


def test_verify_valid_multi_entry_chain() -> None:
    r1 = _make_row(1, GENESIS_HASH)
    r2 = _make_row(2, str(r1["entry_hash"]), result="second-entry")
    r3 = _make_row(3, str(r2["entry_hash"]), tenant_id="licious_coldchain")

    res = verify_audit_chain([r1, r2, r3])
    assert res.valid is True
    assert res.total_entries == 3
    assert res.checked_entries == 3
    assert res.broken_at_id is None


def test_detects_tampered_payload() -> None:
    r1 = _make_row(1, GENESIS_HASH)
    r2 = _make_row(2, str(r1["entry_hash"]), result="original")
    r3 = _make_row(3, str(r2["entry_hash"]))

    r2_tampered = dict(r2)
    r2_tampered["result"] = "tampered-value"

    res = verify_audit_chain([r1, r2_tampered, r3])
    assert res.valid is False
    assert res.broken_at_id == 2
    assert res.checked_entries == 1
    assert "tamper" in res.reason.lower()


def test_detects_broken_prev_hash_link() -> None:
    r1 = _make_row(1, GENESIS_HASH)
    r2 = _make_row(2, "f" * 64)

    res = verify_audit_chain([r1, r2])
    assert res.valid is False
    assert res.broken_at_id == 2
    assert "prev_hash mismatch" in res.reason
