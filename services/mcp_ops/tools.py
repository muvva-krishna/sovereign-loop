from __future__ import annotations

import sys
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError

from services.mcp_erp.db import SessionLocal
from services.mcp_erp.models import OpsTicket
from services.mcp_erp.schemas import OpsTicketResponse
from services.mcp_erp.tools import record_audit_entry

_ALLOWED_SEVERITIES = frozenset({"low", "medium", "high", "critical"})


def _require_non_empty(value: str, field_name: str) -> str:
    cleaned = value.strip() if value else ""
    if not cleaned:
        raise ValueError(f"{field_name} is required and cannot be empty.")
    return cleaned


def _to_ticket_response(ticket: OpsTicket, *, cached_idempotent: bool = False) -> OpsTicketResponse:
    return OpsTicketResponse(
        ticket_id=ticket.ticket_id,
        tenant_id=ticket.tenant_id,
        title=ticket.title,
        severity=ticket.severity,
        related_id=ticket.related_id,
        status=ticket.status,
        idempotency_key=ticket.idempotency_key,
        cached_idempotent=cached_idempotent,
        created_at=ticket.created_at,
    )


def _safe_audit(
    tenant_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    outcome: str,
    result_summary: str,
) -> None:
    try:
        record_audit_entry(
            tenant_id=tenant_id,
            trace_id="mcp-ops-stdio",
            actor_id="mcp_ops",
            tool_name=tool_name,
            arguments_dict=arguments,
            outcome=outcome,
            result_summary=result_summary,
        )
    except Exception as exc:
        print(f"[ops-audit] failed to record entry for {tool_name}: {exc}", file=sys.stderr)


def create_escalation_ticket(
    tenant_id: str,
    title: str,
    severity: str,
    related_id: str = "",
    idempotency_key: str | None = None,
) -> OpsTicketResponse:
    """Create a tenant-scoped operational escalation ticket with optional idempotency."""
    tid = _require_non_empty(tenant_id, "tenant_id")
    clean_title = _require_non_empty(title, "title")
    norm_severity = _require_non_empty(severity, "severity").lower()
    if norm_severity not in _ALLOWED_SEVERITIES:
        raise ValueError(f"Invalid severity '{severity}'. Expected one of {sorted(_ALLOWED_SEVERITIES)}.")

    norm_key = idempotency_key.strip() if idempotency_key and idempotency_key.strip() else None

    with SessionLocal() as db:
        if norm_key is not None:
            existing = (
                db.query(OpsTicket)
                .filter(OpsTicket.tenant_id == tid, OpsTicket.idempotency_key == norm_key)
                .first()
            )
            if existing:
                resp = _to_ticket_response(existing, cached_idempotent=True)
                _safe_audit(
                    tid,
                    "create_escalation_ticket",
                    {
                        "tenant_id": tid,
                        "title": clean_title,
                        "severity": norm_severity,
                        "idempotency_key": norm_key,
                    },
                    "SUCCESS",
                    f"Idempotent hit ticket_id={resp.ticket_id}",
                )
                return resp

        ticket = OpsTicket(
            ticket_id=f"TKT-{uuid.uuid4().hex[:8].upper()}",
            tenant_id=tid,
            title=clean_title,
            severity=norm_severity,
            related_id=related_id.strip() if related_id else "",
            status="open",
            idempotency_key=norm_key,
        )
        db.add(ticket)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            if norm_key is not None:
                existing = (
                    db.query(OpsTicket)
                    .filter(OpsTicket.tenant_id == tid, OpsTicket.idempotency_key == norm_key)
                    .first()
                )
                if existing:
                    return _to_ticket_response(existing, cached_idempotent=True)
            raise

        db.refresh(ticket)
        resp = _to_ticket_response(ticket, cached_idempotent=False)

    _safe_audit(
        tid,
        "create_escalation_ticket",
        {
            "tenant_id": tid,
            "title": clean_title,
            "severity": norm_severity,
            "related_id": related_id,
            "idempotency_key": norm_key,
        },
        "SUCCESS",
        f"Created ticket {resp.ticket_id}",
    )
    return resp


def list_tickets(tenant_id: str, status: str = "open") -> list[OpsTicketResponse]:
    """List tenant-scoped operational tickets filtered by status (or 'all')."""
    tid = _require_non_empty(tenant_id, "tenant_id")
    with SessionLocal() as db:
        query = db.query(OpsTicket).filter(OpsTicket.tenant_id == tid)
        if status and status != "all":
            query = query.filter(OpsTicket.status == status)
        tickets = query.order_by(OpsTicket.created_at.desc(), OpsTicket.ticket_id.asc()).all()
        return [_to_ticket_response(t) for t in tickets]


def send_ops_alert(tenant_id: str, channel: str, message: str) -> dict[str, Any]:
    """Emit an operational alert and persist it to OpsTicket in the shared database."""
    tid = _require_non_empty(tenant_id, "tenant_id")
    clean_channel = _require_non_empty(channel, "channel")
    clean_message = _require_non_empty(message, "message")

    with SessionLocal() as db:
        ticket = OpsTicket(
            ticket_id=f"ALT-{uuid.uuid4().hex[:8].upper()}",
            tenant_id=tid,
            title=f"[{clean_channel}] {clean_message[:180]}",
            severity="alert",
            related_id=clean_channel,
            status="alert_sent",
            idempotency_key=None,
        )
        db.add(ticket)
        db.commit()
        db.refresh(ticket)

    print(f"[ops-alert] tenant={tid} channel={clean_channel} msg={clean_message}", file=sys.stderr)
    _safe_audit(
        tid,
        "send_ops_alert",
        {"tenant_id": tid, "channel": clean_channel, "message": clean_message},
        "SUCCESS",
        f"Sent alert {ticket.ticket_id} to {clean_channel}",
    )
    return {
        "status": "sent",
        "ticket_id": ticket.ticket_id,
        "tenant_id": tid,
        "channel": clean_channel,
        "message": clean_message,
        "created_at": ticket.created_at.isoformat(),
    }
