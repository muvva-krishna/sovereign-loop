from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

from .models import AuditVerifyResult

GENESIS_HASH = "0" * 64


def compute_entry_hash(
    prev_hash: str,
    tenant_id: str,
    trace_id: str,
    actor_id: str,
    tool_name: str,
    arguments_json: str,
    outcome: str,
    result_summary: str,
    timestamp_iso: str,
) -> str:
    """Compute deterministic SHA-256 hash linking an audit record to its predecessor."""
    payload = {
        "prev_hash": prev_hash,
        "tenant_id": tenant_id,
        "trace_id": trace_id,
        "actor_id": actor_id,
        "tool_name": tool_name,
        "arguments": arguments_json,
        "outcome": outcome,
        "result": result_summary,
        "timestamp_iso": timestamp_iso,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def verify_audit_chain(entries: Sequence[Mapping[str, Any]]) -> AuditVerifyResult:
    """Verify hash-chain integrity across ordered audit entries (oldest -> newest)."""
    total = len(entries)
    if total == 0:
        return AuditVerifyResult(valid=True, total_entries=0, checked_entries=0, reason="Empty audit log")

    expected_prev = GENESIS_HASH
    checked = 0
    for row in entries:
        entry_id = int(row["id"])
        if row["prev_hash"] != expected_prev:
            return AuditVerifyResult(
                valid=False,
                total_entries=total,
                checked_entries=checked,
                broken_at_id=entry_id,
                reason=f"prev_hash mismatch at id={entry_id}",
            )

        recomputed = compute_entry_hash(
            prev_hash=str(row["prev_hash"]),
            tenant_id=str(row["tenant_id"]),
            trace_id=str(row["trace_id"]),
            actor_id=str(row["actor_id"]),
            tool_name=str(row["tool_name"]),
            arguments_json=str(row["arguments"]),
            outcome=str(row["outcome"]),
            result_summary=str(row["result"]),
            timestamp_iso=str(row["timestamp_iso"]),
        )
        if row["entry_hash"] != recomputed:
            return AuditVerifyResult(
                valid=False,
                total_entries=total,
                checked_entries=checked,
                broken_at_id=entry_id,
                reason=f"entry_hash tamper detected at id={entry_id}",
            )
        expected_prev = str(row["entry_hash"])
        checked += 1

    return AuditVerifyResult(
        valid=True,
        total_entries=total,
        checked_entries=checked,
        reason="Hash chain verified intact",
    )
