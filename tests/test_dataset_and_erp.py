"""Integration tests for seeded dataset invariants, ERP tools, Knowledge SOPs, and Ops tickets."""

from __future__ import annotations

from pathlib import Path

import pytest

from dataset.seed import verify_seed_invariants
from services.mcp_erp.tools import (
    approve_requisition,
    check_inventory_level,
    create_purchase_requisition,
    get_distributor_profile,
    get_open_sales_orders,
    get_sales_summary,
    list_pending_requisitions,
    reject_requisition,
    verify_all_audit_entries,
)
from services.mcp_knowledge.search import get_sop_document, search_sops
from services.mcp_ops.tools import create_escalation_ticket, list_tickets


def test_seed_invariants_exact_counts(seeded_db: Path) -> None:
    summary = verify_seed_invariants()
    assert summary["distributors"] == 500
    assert summary["skus"] == 200
    assert summary["sales_orders"] == 5200
    assert summary["purchase_requisitions"] >= 6
    assert summary["ops_tickets"] >= 2
    assert summary["low_stock_skus"]["havells_electricals"] == 25
    assert summary["low_stock_skus"]["licious_coldchain"] == 25
    assert summary["audit_chain_valid"] is True


def test_erp_cross_tenant_isolation_on_inventory_and_distributors(seeded_db: Path) -> None:
    inv = check_inventory_level("havells_electricals", "HV-FAN-001")
    assert inv.tenant_id == "havells_electricals"
    assert inv.material_id == "HV-FAN-001"

    with pytest.raises(ValueError, match="not found for tenant"):
        check_inventory_level("licious_coldchain", "HV-FAN-001")

    with pytest.raises(ValueError, match="not found for tenant"):
        get_distributor_profile("havells_electricals", "LC-DIST-0001")

    orders = get_open_sales_orders("havells_electricals", status="open", limit=5)
    assert orders
    assert all(o.tenant_id == "havells_electricals" for o in orders)

    summary = get_sales_summary("licious_coldchain", group_by="city")
    assert summary.total_orders == 2600


def test_requisition_idempotency_and_out_of_band_token_approval(seeded_db: Path) -> None:
    idem_key = "test-idem-req-001"
    first = create_purchase_requisition(
        tenant_id="havells_electricals",
        material_id="HV-SWT-026",
        quantity=150,
        requested_by="hv-mgr-1",
        idempotency_key=idem_key,
        trace_id="trc-idem-1",
    )
    second = create_purchase_requisition(
        tenant_id="havells_electricals",
        material_id="HV-SWT-026",
        quantity=150,
        requested_by="hv-mgr-1",
        idempotency_key=idem_key,
        trace_id="trc-idem-2",
    )
    assert first.requisition_id == second.requisition_id
    assert first.cached_idempotent is False
    assert second.cached_idempotent is True
    assert second.status == "pending_approval"

    pending = list_pending_requisitions("havells_electricals")
    target = next(r for r in pending if r["requisition_id"] == first.requisition_id)

    with pytest.raises(ValueError, match="Invalid or missing approval token"):
        approve_requisition(
            tenant_id="havells_electricals",
            requisition_id=first.requisition_id,
            approved_by="hv-mgr-1",
            approval_token="wrong-token",
        )

    approved = approve_requisition(
        tenant_id="havells_electricals",
        requisition_id=first.requisition_id,
        approved_by="hv-mgr-1",
        approval_token=str(target["approval_token"]),
    )
    assert approved.status == "approved"

    # Reject a separate requisition cleanly
    to_reject = create_purchase_requisition(
        tenant_id="licious_coldchain",
        material_id="LC-CHICK-001",
        quantity=120,
        requested_by="lc-mgr-1",
        idempotency_key="test-idem-reject-001",
        trace_id="trc-rej-1",
    )
    rejected = reject_requisition(
        tenant_id="licious_coldchain",
        requisition_id=to_reject.requisition_id,
        rejected_by="lc-mgr-1",
        reason="Excess stock already in transit",
    )
    assert rejected.status == "rejected"
    assert "transit" in (rejected.rejected_reason or "")


def test_knowledge_sop_search_enforces_tenant_scope(seeded_db: Path) -> None:
    hits = search_sops("havells_electricals", "electrical warranty defective batch SLA", top_k=3)
    assert hits
    assert all(h.tenant_scope in {"all", "havells_electricals"} for h in hits)

    lc_hits = search_sops("licious_coldchain", "cold chain temperature SLA perishable", top_k=3)
    assert lc_hits
    assert all(h.tenant_scope in {"all", "licious_coldchain"} for h in lc_hits)

    # Tenant-specific SOPs cannot be read cross-tenant
    with pytest.raises(ValueError, match="not found for tenant"):
        get_sop_document("licious_coldchain", "SOP-007")
    with pytest.raises(ValueError, match="not found for tenant"):
        get_sop_document("havells_electricals", "SOP-009")


def test_ops_ticket_idempotency_and_audit_integrity(seeded_db: Path) -> None:
    t1 = create_escalation_ticket(
        tenant_id="havells_electricals",
        title="Delayed Mumbai dispatch",
        severity="high",
        related_id="ORD-0001",
        idempotency_key="ops-idem-1",
    )
    t2 = create_escalation_ticket(
        tenant_id="havells_electricals",
        title="Delayed Mumbai dispatch",
        severity="high",
        related_id="ORD-0001",
        idempotency_key="ops-idem-1",
    )
    assert t1.ticket_id == t2.ticket_id
    assert t1.cached_idempotent is False
    assert t2.cached_idempotent is True

    open_tickets = list_tickets("havells_electricals", status="open")
    assert any(t.ticket_id == t1.ticket_id for t in open_tickets)

    audit_check = verify_all_audit_entries()
    assert audit_check.valid is True
