from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class DistributorProfileResponse(BaseModel):
    """Distributor profile including PII fields for gateway redaction verification."""

    model_config = ConfigDict(from_attributes=True)

    distributor_id: str
    tenant_id: str
    name: str
    city: str
    region: str
    phone: str
    email: str
    gstin: str
    credit_limit: float
    tier: str


class InventoryCheckResponse(BaseModel):
    """Inventory item stock snapshot for a tenant SKU."""

    model_config = ConfigDict(from_attributes=True)

    tenant_id: str
    material_id: str
    description: str
    category: str
    warehouse: str
    city: str
    unit_price: float
    quantity_on_hand: int
    reorder_point: int
    below_reorder_point: bool


class SalesOrderResponse(BaseModel):
    """Tenant-scoped sales order view."""

    model_config = ConfigDict(from_attributes=True)

    order_id: str
    tenant_id: str
    distributor_id: str
    customer_name: str
    city: str
    status: str
    order_date: date
    total_value: float
    material_id: str


class SalesSummaryItem(BaseModel):
    """Single aggregation bucket for sales summary."""

    group: str
    order_count: int
    total_revenue: float


class SalesSummaryResponse(BaseModel):
    """Aggregated sales order count and revenue by city or status."""

    tenant_id: str
    group_by: str
    total_orders: int
    total_revenue: float
    breakdown: list[SalesSummaryItem] = Field(default_factory=list)


class PurchaseRequisitionResponse(BaseModel):
    """Agent-safe purchase requisition response (approval_token intentionally omitted)."""

    model_config = ConfigDict(from_attributes=True)

    requisition_id: str
    tenant_id: str
    material_id: str
    quantity: int
    unit_price: float
    total_value: float
    requested_by: str
    status: str
    rejected_reason: str | None = None
    trace_id: str = "unknown"
    idempotency_key: str | None = None
    cached_idempotent: bool = False
    created_at: datetime
    approved_at: datetime | None = None


class OpsTicketResponse(BaseModel):
    """Operational escalation ticket view."""

    model_config = ConfigDict(from_attributes=True)

    ticket_id: str
    tenant_id: str
    title: str
    severity: str
    related_id: str
    status: str
    idempotency_key: str | None = None
    cached_idempotent: bool = False
    created_at: datetime


class AuditLogEntryResponse(BaseModel):
    """Hash-chained audit log record."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: str
    trace_id: str
    actor_id: str
    tool_name: str
    arguments: str
    outcome: str
    result: str
    timestamp_iso: str
    prev_hash: str
    entry_hash: str
