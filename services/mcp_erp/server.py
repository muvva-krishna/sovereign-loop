from __future__ import annotations

import os
import secrets
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from contracts.config import get_settings

from .db import SessionLocal, init_db
from .models import PurchaseRequisition
from .schemas import (
    DistributorProfileResponse,
    InventoryCheckResponse,
    PurchaseRequisitionResponse,
    SalesOrderResponse,
    SalesSummaryResponse,
)
from .tools import (
    approve_requisition,
    check_inventory_level,
    create_purchase_requisition,
    get_open_sales_orders,
    list_audit_entries,
    list_low_stock_items,
    list_pending_requisitions,
    record_audit_entry,
    reject_requisition,
    verify_all_audit_entries,
)
from .tools import (
    get_distributor_profile as _get_distributor_profile,
)
from .tools import (
    get_sales_summary as _get_sales_summary,
)


def _resolve_mcp_api_key() -> str:
    if "MCP_API_KEY" in os.environ:
        return os.environ["MCP_API_KEY"]
    return get_settings().mcp_api_key


def _resolve_admin_api_key() -> str:
    if "ADMIN_API_KEY" in os.environ:
        return os.environ["ADMIN_API_KEY"]
    return get_settings().admin_api_key


class MCPAuthMiddleware:
    """ASGI middleware enforcing MCP_API_KEY on /mcp endpoints (fails closed if unset)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                expected_key = _resolve_mcp_api_key()
                if not expected_key:
                    res = JSONResponse(
                        {"error": "Server configuration error: MCP_API_KEY not set"},
                        status_code=500,
                    )
                    await res(scope, receive, send)
                    return

                headers = Headers(scope=scope)
                auth_header = headers.get("authorization", "")
                token: str | None = None
                if auth_header.lower().startswith("bearer "):
                    token = auth_header[7:].strip()
                elif "x-mcp-api-key" in headers:
                    token = headers.get("x-mcp-api-key")

                if not token or not secrets.compare_digest(token, expected_key):
                    res = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await res(scope, receive, send)
                    return

        await self.app(scope, receive, send)


class AuthFastMCP(FastMCP):
    """FastMCP subclass wrapping Streamable HTTP transport with MCPAuthMiddleware."""

    def streamable_http_app(self) -> Any:
        app = super().streamable_http_app()
        return MCPAuthMiddleware(app)


_MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
_MCP_PORT = int(os.environ.get("ERP_MCP_PORT", os.environ.get("MCP_PORT", "8101")))

mcp = AuthFastMCP("sovereign-erp", host=_MCP_HOST, port=_MCP_PORT)


def _safe_audit(
    tenant_id: str,
    trace_id: str,
    actor_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    outcome: str,
    result_summary: str,
) -> None:
    try:
        record_audit_entry(
            tenant_id=tenant_id or "unknown",
            trace_id=trace_id or "unknown",
            actor_id=actor_id or "mcp_client",
            tool_name=tool_name,
            arguments_dict=arguments,
            outcome=outcome,
            result_summary=result_summary,
        )
    except Exception as exc:
        print(f"[audit] failed to record entry for {tool_name}: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_open_orders(
    tenant_id: str,
    status: str = "open",
    city: str | None = None,
    limit: int = 20,
) -> list[SalesOrderResponse]:
    """Retrieve tenant-scoped sales orders filtered by status and optional city."""
    args = {"tenant_id": tenant_id, "status": status, "city": city, "limit": limit}
    try:
        result = get_open_sales_orders(tenant_id=tenant_id, status=status, city=city, limit=limit)
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "get_open_orders",
            args,
            "SUCCESS",
            f"Returned {len(result)} orders",
        )
        return result
    except Exception as exc:
        _safe_audit(tenant_id, "mcp", "mcp_client", "get_open_orders", args, "ERROR", f"FAILED: {exc}")
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_sales_summary(tenant_id: str, group_by: str = "city") -> SalesSummaryResponse:
    """Return aggregated order count and revenue by city or status for a tenant."""
    args = {"tenant_id": tenant_id, "group_by": group_by}
    try:
        result = _get_sales_summary(tenant_id=tenant_id, group_by=group_by)
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "get_sales_summary",
            args,
            "SUCCESS",
            f"orders={result.total_orders}, revenue={result.total_revenue}",
        )
        return result
    except Exception as exc:
        _safe_audit(tenant_id, "mcp", "mcp_client", "get_sales_summary", args, "ERROR", f"FAILED: {exc}")
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def check_inventory(tenant_id: str, material_id: str) -> InventoryCheckResponse:
    """Check the inventory level for a specific tenant-scoped material ID."""
    args = {"tenant_id": tenant_id, "material_id": material_id}
    try:
        result = check_inventory_level(tenant_id=tenant_id, material_id=material_id)
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "check_inventory",
            args,
            "SUCCESS",
            f"material={material_id}, qty={result.quantity_on_hand}",
        )
        return result
    except Exception as exc:
        _safe_audit(tenant_id, "mcp", "mcp_client", "check_inventory", args, "ERROR", f"FAILED: {exc}")
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_low_stock_items(
    tenant_id: str,
    city: str | None = None,
    limit: int = 20,
) -> list[InventoryCheckResponse]:
    """List tenant inventory items below their reorder point, optionally filtered by city."""
    args = {"tenant_id": tenant_id, "city": city, "limit": limit}
    try:
        result = list_low_stock_items(tenant_id=tenant_id, city=city, limit=limit)
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "get_low_stock_items",
            args,
            "SUCCESS",
            f"Returned {len(result)} low-stock items",
        )
        return result
    except Exception as exc:
        _safe_audit(tenant_id, "mcp", "mcp_client", "get_low_stock_items", args, "ERROR", f"FAILED: {exc}")
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_distributor_profile(tenant_id: str, distributor_id: str) -> DistributorProfileResponse:
    """Retrieve a tenant-scoped distributor profile including contact and GSTIN details."""
    args = {"tenant_id": tenant_id, "distributor_id": distributor_id}
    try:
        result = _get_distributor_profile(tenant_id=tenant_id, distributor_id=distributor_id)
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "get_distributor_profile",
            args,
            "SUCCESS",
            f"Returned distributor {distributor_id}",
        )
        return result
    except Exception as exc:
        _safe_audit(
            tenant_id,
            "mcp",
            "mcp_client",
            "get_distributor_profile",
            args,
            "ERROR",
            f"FAILED: {exc}",
        )
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def create_requisition(
    tenant_id: str,
    material_id: str,
    quantity: int,
    requested_by: str,
    idempotency_key: str | None = None,
    trace_id: str = "unknown",
) -> PurchaseRequisitionResponse:
    """Create a pending_approval purchase requisition (approval_token is withheld)."""
    args = {
        "tenant_id": tenant_id,
        "material_id": material_id,
        "quantity": quantity,
        "requested_by": requested_by,
        "idempotency_key": idempotency_key,
        "trace_id": trace_id,
    }
    try:
        req = create_purchase_requisition(
            tenant_id=tenant_id,
            material_id=material_id,
            quantity=quantity,
            requested_by=requested_by,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
        )
        _safe_audit(
            tenant_id,
            trace_id,
            requested_by,
            "create_requisition",
            args,
            "PENDING_APPROVAL",
            f"requisition_id={req.requisition_id}, cached_idempotent={req.cached_idempotent}",
        )
        return req
    except Exception as exc:
        _safe_audit(tenant_id, trace_id, requested_by, "create_requisition", args, "ERROR", f"FAILED: {exc}")
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def approve_pending_requisition(
    tenant_id: str,
    requisition_id: str,
    approved_by: str,
    approval_token: str,
) -> PurchaseRequisitionResponse:
    """Approve a pending purchase requisition using an out-of-band approval_token."""
    args = {
        "tenant_id": tenant_id,
        "requisition_id": requisition_id,
        "approved_by": approved_by,
        "approval_token": approval_token,
    }
    try:
        req = approve_requisition(
            tenant_id=tenant_id,
            requisition_id=requisition_id,
            approved_by=approved_by,
            approval_token=approval_token,
        )
        _safe_audit(
            tenant_id,
            req.trace_id,
            approved_by,
            "approve_pending_requisition",
            args,
            "SUCCESS",
            f"Approved requisition {req.requisition_id}",
        )
        return req
    except Exception as exc:
        _safe_audit(
            tenant_id,
            "mcp",
            approved_by,
            "approve_pending_requisition",
            args,
            "ERROR",
            f"FAILED: {exc}",
        )
        raise


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def reject_pending_requisition(
    tenant_id: str,
    requisition_id: str,
    rejected_by: str,
    reason: str = "",
) -> PurchaseRequisitionResponse:
    """Reject a pending purchase requisition."""
    args = {
        "tenant_id": tenant_id,
        "requisition_id": requisition_id,
        "rejected_by": rejected_by,
        "reason": reason,
    }
    try:
        req = reject_requisition(
            tenant_id=tenant_id,
            requisition_id=requisition_id,
            rejected_by=rejected_by,
            reason=reason,
        )
        _safe_audit(
            tenant_id,
            req.trace_id,
            rejected_by,
            "reject_pending_requisition",
            args,
            "SUCCESS",
            f"Rejected requisition {req.requisition_id}",
        )
        return req
    except Exception as exc:
        _safe_audit(
            tenant_id,
            "mcp",
            rejected_by,
            "reject_pending_requisition",
            args,
            "ERROR",
            f"FAILED: {exc}",
        )
        raise


# ---------------------------------------------------------------------------
# Admin & Health REST routes
# ---------------------------------------------------------------------------


def _check_admin_auth(request: Request) -> JSONResponse | None:
    admin_key = _resolve_admin_api_key()
    if not admin_key:
        return JSONResponse({"error": "Server configuration error: ADMIN_API_KEY not set"}, status_code=500)
    client_key = request.headers.get("X-Admin-Key")
    if not client_key or not secrets.compare_digest(client_key, admin_key):
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    return None


async def _parse_json_body(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _resolve_requisition_tenant(requisition_id: str, explicit_tenant_id: str | None) -> str:
    if explicit_tenant_id and explicit_tenant_id.strip():
        return explicit_tenant_id.strip()
    with SessionLocal() as db:
        req = (
            db.query(PurchaseRequisition)
            .filter(PurchaseRequisition.requisition_id == requisition_id)
            .first()
        )
        if not req:
            raise ValueError(f"Requisition {requisition_id} not found.")
        return req.tenant_id


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Service liveness endpoint."""
    return JSONResponse({"status": "ok", "service": "mcp_erp"})


@mcp.custom_route("/admin/pending-requisitions", methods=["GET"])
async def admin_pending_requisitions(request: Request) -> JSONResponse:
    """Admin-only endpoint returning pending requisitions and approval tokens."""
    auth_error = _check_admin_auth(request)
    if auth_error:
        return auth_error
    tenant_id = request.query_params.get("tenant_id")
    pending = list_pending_requisitions(tenant_id=tenant_id)
    return JSONResponse({"pending_requisitions": pending, "count": len(pending)})


@mcp.custom_route("/admin/requisitions/{requisition_id}/approve", methods=["POST"])
async def admin_approve_requisition(request: Request) -> JSONResponse:
    """Admin-only out-of-band approval endpoint."""
    auth_error = _check_admin_auth(request)
    if auth_error:
        return auth_error

    requisition_id = request.path_params["requisition_id"]
    body = await _parse_json_body(request)
    approved_by = str(body.get("approved_by") or "admin")
    approval_token = str(body.get("approval_token") or "")
    explicit_tenant = body.get("tenant_id") or request.query_params.get("tenant_id")

    try:
        tenant_id = _resolve_requisition_tenant(requisition_id, explicit_tenant)
        req = approve_requisition(
            tenant_id=tenant_id,
            requisition_id=requisition_id,
            approved_by=approved_by,
            approval_token=approval_token,
        )
        _safe_audit(
            tenant_id,
            req.trace_id,
            approved_by,
            "admin_approve_requisition",
            {"tenant_id": tenant_id, "requisition_id": requisition_id, "approval_token": approval_token},
            "APPROVED",
            f"Admin approved {requisition_id}",
        )
        return JSONResponse(req.model_dump(mode="json"))
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        return JSONResponse({"error": str(exc)}, status_code=status_code)


@mcp.custom_route("/admin/requisitions/{requisition_id}/reject", methods=["POST"])
async def admin_reject_requisition(request: Request) -> JSONResponse:
    """Admin-only out-of-band rejection endpoint."""
    auth_error = _check_admin_auth(request)
    if auth_error:
        return auth_error

    requisition_id = request.path_params["requisition_id"]
    body = await _parse_json_body(request)
    rejected_by = str(body.get("rejected_by") or "admin")
    reason = str(body.get("reason") or "")
    explicit_tenant = body.get("tenant_id") or request.query_params.get("tenant_id")

    try:
        tenant_id = _resolve_requisition_tenant(requisition_id, explicit_tenant)
        req = reject_requisition(
            tenant_id=tenant_id,
            requisition_id=requisition_id,
            rejected_by=rejected_by,
            reason=reason,
        )
        _safe_audit(
            tenant_id,
            req.trace_id,
            rejected_by,
            "admin_reject_requisition",
            {"tenant_id": tenant_id, "requisition_id": requisition_id, "reason": reason},
            "REJECTED",
            f"Admin rejected {requisition_id}",
        )
        return JSONResponse(req.model_dump(mode="json"))
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc).lower() else 400
        return JSONResponse({"error": str(exc)}, status_code=status_code)


@mcp.custom_route("/admin/audit-log", methods=["GET"])
async def admin_audit_log(request: Request) -> JSONResponse:
    """Admin-only endpoint returning hash-chained audit entries."""
    auth_error = _check_admin_auth(request)
    if auth_error:
        return auth_error

    tenant_id = request.query_params.get("tenant_id")
    try:
        limit = max(1, min(int(request.query_params.get("limit", "100")), 500))
    except (ValueError, TypeError):
        limit = 100

    entries = list_audit_entries(tenant_id=tenant_id, limit=limit)
    return JSONResponse({"audit_log": entries, "count": len(entries), "limit": limit})


@mcp.custom_route("/admin/audit/verify", methods=["GET"])
async def admin_audit_verify(request: Request) -> JSONResponse:
    """Admin-only endpoint verifying SHA-256 hash-chain integrity across all audit entries."""
    auth_error = _check_admin_auth(request)
    if auth_error:
        return auth_error
    result = verify_all_audit_entries()
    return JSONResponse(result.model_dump())


if __name__ == "__main__":
    init_db()
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
