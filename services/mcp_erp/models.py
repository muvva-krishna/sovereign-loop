from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Distributor(Base):
    """Multi-tenant distributor master record including PII fields."""

    __tablename__ = "distributors"

    distributor_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    region: Mapped[str] = mapped_column(String(100), nullable=False)
    phone: Mapped[str] = mapped_column(String(64), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    gstin: Mapped[str] = mapped_column(String(32), nullable=False)
    credit_limit: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    tier: Mapped[str] = mapped_column(String(32), nullable=False)


class InventoryItem(Base):
    """Tenant-scoped inventory SKU and warehouse stock levels."""

    __tablename__ = "inventory_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "material_id", name="uq_inventory_tenant_material"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    material_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    warehouse: Mapped[str] = mapped_column(String(64), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    quantity_on_hand: Mapped[int] = mapped_column(Integer, nullable=False)
    reorder_point: Mapped[int] = mapped_column(Integer, nullable=False)


class SalesOrder(Base):
    """Tenant-scoped distributor sales order."""

    __tablename__ = "sales_orders"

    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    distributor_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("distributors.distributor_id"), nullable=False, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False)
    city: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    order_date: Mapped[date] = mapped_column(Date, nullable=False)
    total_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    material_id: Mapped[str] = mapped_column(String(64), nullable=False)


class PurchaseRequisition(Base):
    """Tenant-scoped purchase requisition with out-of-band approval token."""

    __tablename__ = "purchase_requisitions"
    __table_args__ = (
        Index("ix_pr_tenant_idempotency", "tenant_id", "idempotency_key", unique=True),
    )

    requisition_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    unit_price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False)
    total_value: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    requested_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending_approval")
    approval_token: Mapped[str | None] = mapped_column(String(128), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class OpsTicket(Base):
    """Tenant-scoped operational escalation ticket or alert record."""

    __tablename__ = "ops_tickets"
    __table_args__ = (
        Index("ix_ops_ticket_tenant_idempotency", "tenant_id", "idempotency_key", unique=True),
    )

    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    severity: Mapped[str] = mapped_column(String(32), nullable=False)
    related_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="open")
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)


class AuditLog(Base):
    """Append-only SHA-256 hash-chained audit trail."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False)
    arguments: Mapped[str] = mapped_column(Text, nullable=False)
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
    result: Mapped[str] = mapped_column(Text, nullable=False)
    timestamp_iso: Mapped[str] = mapped_column(String(64), nullable=False)
    prev_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
