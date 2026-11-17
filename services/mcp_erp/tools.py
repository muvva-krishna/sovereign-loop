from __future__ import annotations

import json
import secrets
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from contracts.audit_crypto import GENESIS_HASH, compute_entry_hash, verify_audit_chain
from contracts.models import AuditVerifyResult

from .db import SessionLocal
from .models import (
    AuditLog,
    Distributor,
    InventoryItem,
    PurchaseRequisition,
    SalesOrder,
)
from .schemas import (
    AuditLogEntryResponse,
    DistributorProfileResponse,
    InventoryCheckResponse,
    PurchaseRequisitionResponse,
    SalesOrderResponse,
    SalesSummaryItem,
    SalesSummaryResponse,
)

_SENSITIVE_ARG_KEYS = {"approval_token", "api_key", "admin_key", "password", "secret"}
_AUDIT_LOCK = threading.Lock()


def _require_tenant_id(tenant_id: str) -> str:
    cleaned = tenant_id.strip() if tenant_id else ""
    if not cleaned:
        raise ValueError("tenant_id is required and cannot be empty.")
    return cleaned


def _redact_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of arguments with sensitive keys redacted recursively."""
    redacted: dict[str, Any] = {}
    for key, value in arguments.items():
        if key.lower() in _SENSITIVE_ARG_KEYS:
            redacted[key] = "***REDACTED***"
        elif isinstance(value, dict):
            redacted[key] = _redact_arguments(value)
        else:
            redacted[key] = value
    return redacted


def _to_inventory_response(item: InventoryItem) -> InventoryCheckResponse:
    return InventoryCheckResponse(
        tenant_id=item.tenant_id,
        material_id=item.material_id,
        description=item.description,
        category=item.category,
        warehouse=item.warehouse,
        city=item.city,
        unit_price=float(item.unit_price),
        quantity_on_hand=item.quantity_on_hand,
        reorder_point=item.reorder_point,
        below_reorder_point=item.quantity_on_hand < item.reorder_point,
    )


def _to_requisition_response(
    req: PurchaseRequisition, *, cached_idempotent: bool = False
) -> PurchaseRequisitionResponse:
    return PurchaseRequisitionResponse(
        requisition_id=req.requisition_id,
        tenant_id=req.tenant_id,
        material_id=req.material_id,
        quantity=req.quantity,
        unit_price=float(req.unit_price),
        total_value=float(req.total_value),
        requested_by=req.requested_by,
        status=req.status,
        rejected_reason=req.rejected_reason,
        trace_id=req.trace_id,
        idempotency_key=req.idempotency_key,
        cached_idempotent=cached_idempotent,
        created_at=req.created_at,
        approved_at=req.approved_at,
    )


def record_audit_entry(
    tenant_id: str,
    trace_id: str,
    actor_id: str,
    tool_name: str,
    arguments_dict: dict[str, Any],
    outcome: str,
    result_summary: str,
) -> AuditLogEntryResponse:
    """Append a hash-chained audit record, redacting sensitive argument fields."""
    safe_args = _redact_arguments(arguments_dict)
    arguments_json = json.dumps(safe_args, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    timestamp_iso = datetime.now(timezone.utc).isoformat()
    trimmed_result = str(result_summary)[:2000]

    with _AUDIT_LOCK:
        with SessionLocal() as db:
            last_entry = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
            prev_hash = last_entry.entry_hash if last_entry else GENESIS_HASH
            entry_hash = compute_entry_hash(
                prev_hash=prev_hash,
                tenant_id=tenant_id,
                trace_id=trace_id,
                actor_id=actor_id,
                tool_name=tool_name,
                arguments_json=arguments_json,
                outcome=outcome,
                result_summary=trimmed_result,
                timestamp_iso=timestamp_iso,
            )
            entry = AuditLog(
                tenant_id=tenant_id,
                trace_id=trace_id,
                actor_id=actor_id,
                tool_name=tool_name,
                arguments=arguments_json,
                outcome=outcome,
                result=trimmed_result,
                timestamp_iso=timestamp_iso,
                prev_hash=prev_hash,
                entry_hash=entry_hash,
            )
            db.add(entry)
            db.commit()
            db.refresh(entry)
            return AuditLogEntryResponse.model_validate(entry)


def get_open_sales_orders(
    tenant_id: str,
    status: str = "open",
    city: str | None = None,
    limit: int = 20,
) -> list[SalesOrderResponse]:
    """Retrieve tenant-scoped sales orders filtered by status and optional city."""
    tid = _require_tenant_id(tenant_id)
    clamped_limit = max(1, min(limit, 200))

    with SessionLocal() as db:
        query = db.query(SalesOrder).filter(
            SalesOrder.tenant_id == tid,
            SalesOrder.status == status,
        )
        if city and city.strip():
            query = query.filter(func.lower(SalesOrder.city) == city.strip().lower())

        orders = (
            query.order_by(SalesOrder.order_date.desc(), SalesOrder.order_id.asc())
            .limit(clamped_limit)
            .all()
        )
        return [SalesOrderResponse.model_validate(order) for order in orders]


def get_sales_summary(tenant_id: str, group_by: str = "city") -> SalesSummaryResponse:
    """Return aggregated order count and revenue grouped by city or status for a tenant."""
    tid = _require_tenant_id(tenant_id)
    if group_by not in {"city", "status"}:
        raise ValueError("group_by must be 'city' or 'status'.")

    group_col = SalesOrder.city if group_by == "city" else SalesOrder.status
    with SessionLocal() as db:
        rows = (
            db.query(
                group_col.label("group_key"),
                func.count(SalesOrder.order_id).label("order_count"),
                func.coalesce(func.sum(SalesOrder.total_value), 0).label("total_revenue"),
            )
            .filter(SalesOrder.tenant_id == tid)
            .group_by(group_col)
            .all()
        )

        breakdown = [
            SalesSummaryItem(
                group=str(row.group_key),
                order_count=int(row.order_count),
                total_revenue=round(float(row.total_revenue), 2),
            )
            for row in rows
        ]
        breakdown.sort(key=lambda item: (-item.total_revenue, item.group))
        total_orders = sum(item.order_count for item in breakdown)
        total_revenue = round(sum(item.total_revenue for item in breakdown), 2)

        return SalesSummaryResponse(
            tenant_id=tid,
            group_by=group_by,
            total_orders=total_orders,
            total_revenue=total_revenue,
            breakdown=breakdown,
        )


def check_inventory_level(tenant_id: str, material_id: str) -> InventoryCheckResponse:
    """Check inventory stock level for a tenant-scoped material_id."""
    tid = _require_tenant_id(tenant_id)
    with SessionLocal() as db:
        item = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.tenant_id == tid,
                InventoryItem.material_id == material_id,
            )
            .first()
        )
        if not item:
            raise ValueError(f"Material {material_id} not found for tenant {tid}.")
        return _to_inventory_response(item)


def list_low_stock_items(
    tenant_id: str,
    city: str | None = None,
    limit: int = 20,
) -> list[InventoryCheckResponse]:
    """List tenant inventory items where quantity_on_hand is below reorder_point."""
    tid = _require_tenant_id(tenant_id)
    clamped_limit = max(1, min(limit, 200))

    with SessionLocal() as db:
        query = db.query(InventoryItem).filter(
            InventoryItem.tenant_id == tid,
            InventoryItem.quantity_on_hand < InventoryItem.reorder_point,
        )
        if city and city.strip():
            query = query.filter(func.lower(InventoryItem.city) == city.strip().lower())

        items = (
            query.order_by(
                (InventoryItem.reorder_point - InventoryItem.quantity_on_hand).desc(),
                InventoryItem.material_id.asc(),
            )
            .limit(clamped_limit)
            .all()
        )
        return [_to_inventory_response(item) for item in items]


def get_distributor_profile(tenant_id: str, distributor_id: str) -> DistributorProfileResponse:
    """Retrieve a tenant-scoped distributor profile including contact and tax identifiers."""
    tid = _require_tenant_id(tenant_id)
    with SessionLocal() as db:
        dist = (
            db.query(Distributor)
            .filter(
                Distributor.tenant_id == tid,
                Distributor.distributor_id == distributor_id,
            )
            .first()
        )
        if not dist:
            raise ValueError(f"Distributor {distributor_id} not found for tenant {tid}.")
        return DistributorProfileResponse.model_validate(dist)


def create_purchase_requisition(
    tenant_id: str,
    material_id: str,
    quantity: int,
    requested_by: str,
    idempotency_key: str | None = None,
    trace_id: str = "unknown",
) -> PurchaseRequisitionResponse:
    """Create a pending_approval requisition with idempotency deduplication."""
    tid = _require_tenant_id(tenant_id)
    if quantity <= 0:
        raise ValueError("Quantity must be positive.")

    norm_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None

    with SessionLocal() as db:
        if norm_key is not None:
            existing = (
                db.query(PurchaseRequisition)
                .filter(
                    PurchaseRequisition.tenant_id == tid,
                    PurchaseRequisition.idempotency_key == norm_key,
                )
                .first()
            )
            if existing:
                return _to_requisition_response(existing, cached_idempotent=True)

        item = (
            db.query(InventoryItem)
            .filter(
                InventoryItem.tenant_id == tid,
                InventoryItem.material_id == material_id,
            )
            .first()
        )
        if not item:
            raise ValueError(f"Material {material_id} not found for tenant {tid}.")

        unit_price = float(item.unit_price)
        total_value = round(unit_price * quantity, 2)
        requisition_id = f"PR-{uuid.uuid4().hex[:8].upper()}"
        approval_token = secrets.token_urlsafe(16)

        new_req = PurchaseRequisition(
            requisition_id=requisition_id,
            tenant_id=tid,
            material_id=material_id,
            quantity=quantity,
            unit_price=unit_price,
            total_value=total_value,
            requested_by=requested_by,
            status="pending_approval",
            approval_token=approval_token,
            idempotency_key=norm_key,
            trace_id=trace_id or "unknown",
        )
        db.add(new_req)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if norm_key is not None:
                existing = (
                    db.query(PurchaseRequisition)
                    .filter(
                        PurchaseRequisition.tenant_id == tid,
                        PurchaseRequisition.idempotency_key == norm_key,
                    )
                    .first()
                )
                if existing:
                    return _to_requisition_response(existing, cached_idempotent=True)
            raise

        db.refresh(new_req)
        return _to_requisition_response(new_req, cached_idempotent=False)


def approve_requisition(
    tenant_id: str,
    requisition_id: str,
    approved_by: str,
    approval_token: str,
) -> PurchaseRequisitionResponse:
    """Approve a pending purchase requisition using constant-time token verification."""
    tid = _require_tenant_id(tenant_id)
    with SessionLocal() as db:
        req = (
            db.query(PurchaseRequisition)
            .filter(
                PurchaseRequisition.tenant_id == tid,
                PurchaseRequisition.requisition_id == requisition_id,
            )
            .first()
        )
        if not req:
            raise ValueError(f"Requisition {requisition_id} not found for tenant {tid}.")
        if req.status != "pending_approval":
            raise ValueError(f"Requisition {requisition_id} cannot be approved from status '{req.status}'.")
        if (
            not req.approval_token
            or not approval_token
            or not secrets.compare_digest(req.approval_token, approval_token)
        ):
            raise ValueError(f"Invalid or missing approval token for requisition {requisition_id}.")

        req.status = "approved"
        req.approved_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(req)
        return _to_requisition_response(req)


def reject_requisition(
    tenant_id: str,
    requisition_id: str,
    rejected_by: str,
    reason: str = "",
) -> PurchaseRequisitionResponse:
    """Reject a pending purchase requisition with an optional reason."""
    tid = _require_tenant_id(tenant_id)
    with SessionLocal() as db:
        req = (
            db.query(PurchaseRequisition)
            .filter(
                PurchaseRequisition.tenant_id == tid,
                PurchaseRequisition.requisition_id == requisition_id,
            )
            .first()
        )
        if not req:
            raise ValueError(f"Requisition {requisition_id} not found for tenant {tid}.")
        if req.status != "pending_approval":
            raise ValueError(f"Requisition {requisition_id} cannot be rejected from status '{req.status}'.")

        req.status = "rejected"
        req.rejected_reason = reason.strip() or f"Rejected by {rejected_by}"
        db.commit()
        db.refresh(req)
        return _to_requisition_response(req)


def list_pending_requisitions(tenant_id: str | None = None) -> list[dict[str, Any]]:
    """Admin-only helper returning pending requisitions with approval tokens."""
    with SessionLocal() as db:
        query = db.query(PurchaseRequisition).filter(
            PurchaseRequisition.status == "pending_approval"
        )
        if tenant_id:
            query = query.filter(PurchaseRequisition.tenant_id == tenant_id)
        pending = query.order_by(PurchaseRequisition.created_at.asc()).all()
        return [
            {
                "requisition_id": r.requisition_id,
                "tenant_id": r.tenant_id,
                "material_id": r.material_id,
                "quantity": r.quantity,
                "unit_price": float(r.unit_price),
                "total_value": float(r.total_value),
                "requested_by": r.requested_by,
                "status": r.status,
                "approval_token": r.approval_token,
                "trace_id": r.trace_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in pending
        ]


def list_audit_entries(tenant_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent audit log entries ordered newest-first."""
    clamped_limit = max(1, min(limit, 500))
    with SessionLocal() as db:
        query = db.query(AuditLog)
        if tenant_id:
            query = query.filter(AuditLog.tenant_id == tenant_id)
        entries = query.order_by(AuditLog.id.desc()).limit(clamped_limit).all()
        return [AuditLogEntryResponse.model_validate(entry).model_dump() for entry in entries]


def verify_all_audit_entries() -> AuditVerifyResult:
    """Verify hash-chain integrity across the entire audit log oldest-to-newest."""
    with SessionLocal() as db:
        entries = db.query(AuditLog).order_by(AuditLog.id.asc()).all()
        rows = [
            {
                "id": e.id,
                "tenant_id": e.tenant_id,
                "trace_id": e.trace_id,
                "actor_id": e.actor_id,
                "tool_name": e.tool_name,
                "arguments": e.arguments,
                "outcome": e.outcome,
                "result": e.result,
                "timestamp_iso": e.timestamp_iso,
                "prev_hash": e.prev_hash,
                "entry_hash": e.entry_hash,
            }
            for e in entries
        ]
    return verify_audit_chain(rows)
