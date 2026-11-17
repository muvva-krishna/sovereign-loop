from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from services.mcp_erp.db import init_db
from services.mcp_erp.schemas import OpsTicketResponse

from .tools import (
    create_escalation_ticket as _create_escalation_ticket,
)
from .tools import (
    list_tickets as _list_tickets,
)
from .tools import (
    send_ops_alert as _send_ops_alert,
)

mcp = FastMCP("sovereign-ops")


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def create_escalation_ticket(
    tenant_id: str,
    title: str,
    severity: str,
    related_id: str = "",
    idempotency_key: str | None = None,
) -> OpsTicketResponse:
    """Create a tenant-scoped operational escalation ticket."""
    return _create_escalation_ticket(
        tenant_id=tenant_id,
        title=title,
        severity=severity,
        related_id=related_id,
        idempotency_key=idempotency_key,
    )


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def list_tickets(tenant_id: str, status: str = "open") -> list[OpsTicketResponse]:
    """List tenant-scoped operational tickets filtered by status."""
    return _list_tickets(tenant_id=tenant_id, status=status)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    )
)
def send_ops_alert(tenant_id: str, channel: str, message: str) -> dict[str, Any]:
    """Send an operational alert to a notification channel and persist it."""
    return _send_ops_alert(tenant_id=tenant_id, channel=channel, message=message)


if __name__ == "__main__":
    init_db()
    mcp.run(transport="stdio")
