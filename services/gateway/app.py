"""FastAPI Resilient AI Gateway exposing chat, tools, approvals, audit, traces, and evals."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from loguru import logger
from pydantic import BaseModel

from contracts.config import PROJECT_ROOT, get_settings
from contracts.models import (
    AuditVerifyResult,
    ChatRequest,
    ChatResponse,
    Role,
    TenantContext,
)
from services.gateway.auth import (
    filter_tools_for_role,
    get_tenant_context,
    require_roles,
)
from services.gateway.llm_router import LLMRouter
from services.gateway.mcp_client import MCPClientManager
from services.gateway.observability import LangfuseTracer, TraceRecorder
from services.gateway.orchestrator import Orchestrator
from services.gateway.policy_engine import PolicyEngine
from services.gateway.rate_limiter import RateLimiter

STATIC_INDEX_PATH = Path(__file__).resolve().parent / "static" / "index.html"


class ApprovalActionBody(BaseModel):
    """Optional comment/reason payload for manager approval or rejection."""

    comment: str | None = None
    reason: str | None = None


def _init_database_safe() -> None:
    try:
        from services.mcp_erp.db import init_db

        init_db()
    except Exception as exc:
        logger.warning(f"Database initialization skipped/deferred: {exc}")


async def _ensure_services(target_app: FastAPI) -> None:
    """Lazily populate app.state if endpoints are invoked without lifespan."""
    if getattr(target_app.state, "orchestrator", None) is not None:
        return
    settings = get_settings()
    _init_database_safe()
    policy_engine = PolicyEngine()
    mcp_client = MCPClientManager()
    await mcp_client.initialize()
    llm_router = LLMRouter()
    trace_recorder = TraceRecorder()
    langfuse_tracer = LangfuseTracer()
    rate_limiter = RateLimiter(redis_url=settings.redis_url)
    orchestrator = Orchestrator(
        policy_engine=policy_engine,
        mcp_client=mcp_client,
        llm_router=llm_router,
        trace_recorder=trace_recorder,
        langfuse_tracer=langfuse_tracer,
    )
    target_app.state.policy_engine = policy_engine
    target_app.state.mcp_client = mcp_client
    target_app.state.llm_router = llm_router
    target_app.state.trace_recorder = trace_recorder
    target_app.state.langfuse_tracer = langfuse_tracer
    target_app.state.rate_limiter = rate_limiter
    target_app.state.orchestrator = orchestrator


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    """Initialize DB, MCP sessions, rate limiter, and observability on startup."""
    await _ensure_services(app_instance)
    try:
        yield
    finally:
        mcp_client: MCPClientManager | None = getattr(app_instance.state, "mcp_client", None)
        if mcp_client is not None:
            await mcp_client.cleanup()
        rate_limiter: RateLimiter | None = getattr(app_instance.state, "rate_limiter", None)
        if rate_limiter is not None:
            await rate_limiter.close()
        langfuse_tracer: LangfuseTracer | None = getattr(
            app_instance.state, "langfuse_tracer", None
        )
        if langfuse_tracer is not None:
            langfuse_tracer.shutdown()


app = FastAPI(
    title="Sovereign AI Gateway",
    version="0.1.0",
    description="Multi-tenant AI Gateway with deterministic policies, PII scrubbing, and hash-chained audit.",
    lifespan=lifespan,
)

_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Liveness and capability check."""
    await _ensure_services(request.app)
    settings = get_settings()
    mcp_client: MCPClientManager = request.app.state.mcp_client
    return {
        "status": "ok",
        "llm_mode": settings.llm_mode,
        "mcp_backend": mcp_client.backend_mode,
        "tools_count": len(mcp_client.list_tools()),
    }


@app.get("/", response_model=None)
@app.get("/ui", response_model=None)
async def serve_ui() -> FileResponse | JSONResponse:
    """Serve Ops Console SPA if available, otherwise return JSON status."""
    if STATIC_INDEX_PATH.exists():
        return FileResponse(STATIC_INDEX_PATH)
    return JSONResponse({"service": "sovereign-ai-gateway", "status": "ok", "ui": "not_found"})


@app.post("/v1/chat", response_model=ChatResponse)
async def chat_endpoint(
    request: Request,
    body: ChatRequest,
    ctx: TenantContext = Depends(get_tenant_context),
) -> ChatResponse:
    """Execute authenticated, rate-limited, policy-governed multi-round agent chat."""
    await _ensure_services(request.app)
    rate_limiter: RateLimiter = request.app.state.rate_limiter
    rl_result = await rate_limiter.check_and_consume(
        f"{ctx.tenant_id}:{ctx.user_id}", ctx.rpm_limit
    )
    if not rl_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded ({ctx.rpm_limit} req/min). Retry later.",
            headers={"Retry-After": str(int(max(1, rl_result.retry_after_seconds)))},
        )

    orchestrator: Orchestrator = request.app.state.orchestrator
    return await orchestrator.run_chat(ctx, body)


@app.get("/v1/tools")
async def list_tools_endpoint(
    request: Request,
    ctx: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    """Return MCP tool schemas filtered by caller's RBAC role."""
    await _ensure_services(request.app)
    mcp_client: MCPClientManager = request.app.state.mcp_client
    policy_engine: PolicyEngine = request.app.state.policy_engine
    filtered = filter_tools_for_role(
        ctx.role, mcp_client.list_tools(), policy_engine.role_tools_map
    )
    return {
        "tenant_id": ctx.tenant_id,
        "role": ctx.role.value,
        "tools": filtered,
    }


@app.get("/v1/approvals")
async def list_approvals_endpoint(
    ctx: TenantContext = Depends(require_roles(Role.MANAGER, Role.ADMIN)),
) -> dict[str, Any]:
    """List pending purchase requisitions for the caller's tenant (token stripped)."""
    from services.mcp_erp.tools import list_pending_requisitions

    pending = list_pending_requisitions(tenant_id=ctx.tenant_id)
    sanitized = [
        {
            **{k: v for k, v in item.items() if k != "approval_token"},
            "sku": item.get("material_id"),
        }
        for item in pending
    ]
    return {"tenant_id": ctx.tenant_id, "approvals": sanitized}


@app.post("/v1/approvals/{requisition_id}/approve")
async def approve_requisition_endpoint(
    requisition_id: str,
    body: ApprovalActionBody | None = Body(default=None),
    ctx: TenantContext = Depends(require_roles(Role.MANAGER, Role.ADMIN)),
) -> dict[str, Any]:
    """Human out-of-band approval of a pending requisition for caller's tenant."""
    from services.mcp_erp.tools import (
        approve_requisition,
        list_pending_requisitions,
        record_audit_entry,
    )

    pending = list_pending_requisitions(tenant_id=ctx.tenant_id)
    target = next(
        (r for r in pending if r.get("requisition_id") == requisition_id),
        None,
    )
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pending requisition '{requisition_id}' not found for tenant '{ctx.tenant_id}'.",
        )

    token = str(target.get("approval_token") or "")
    try:
        updated = approve_requisition(
            tenant_id=ctx.tenant_id,
            requisition_id=requisition_id,
            approved_by=ctx.user_id,
            approval_token=token,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    comment = (body.comment or body.reason or "") if body else ""
    record_audit_entry(
        tenant_id=ctx.tenant_id,
        trace_id=updated.trace_id,
        actor_id=ctx.user_id,
        tool_name="admin.approve_requisition",
        arguments_dict={"requisition_id": requisition_id, "comment": comment},
        outcome="APPROVED",
        result_summary=f"Approved requisition {requisition_id} by {ctx.user_id}",
    )
    return updated.model_dump(mode="json")


@app.post("/v1/approvals/{requisition_id}/reject")
async def reject_requisition_endpoint(
    requisition_id: str,
    body: ApprovalActionBody | None = Body(default=None),
    ctx: TenantContext = Depends(require_roles(Role.MANAGER, Role.ADMIN)),
) -> dict[str, Any]:
    """Human out-of-band rejection of a pending requisition for caller's tenant."""
    from services.mcp_erp.tools import record_audit_entry, reject_requisition

    reason = ((body.reason or body.comment) if body else None) or f"Rejected by {ctx.user_id}"
    try:
        updated = reject_requisition(
            tenant_id=ctx.tenant_id,
            requisition_id=requisition_id,
            rejected_by=ctx.user_id,
            reason=reason,
        )
    except ValueError as exc:
        msg = str(exc)
        code = (
            status.HTTP_404_NOT_FOUND
            if "not found" in msg.lower()
            else status.HTTP_400_BAD_REQUEST
        )
        raise HTTPException(status_code=code, detail=msg) from exc

    record_audit_entry(
        tenant_id=ctx.tenant_id,
        trace_id=updated.trace_id,
        actor_id=ctx.user_id,
        tool_name="admin.reject_requisition",
        arguments_dict={"requisition_id": requisition_id, "reason": reason},
        outcome="REJECTED",
        result_summary=f"Rejected requisition {requisition_id}: {reason}",
    )
    return updated.model_dump(mode="json")


@app.get("/v1/audit")
async def list_audit_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    ctx: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    """List hash-chained audit entries scoped to the caller's tenant."""
    from services.mcp_erp.tools import list_audit_entries

    entries = list_audit_entries(tenant_id=ctx.tenant_id, limit=limit)
    return {"tenant_id": ctx.tenant_id, "entries": entries}


@app.get("/v1/audit/verify", response_model=AuditVerifyResult)
async def verify_audit_endpoint(
    _ctx: TenantContext = Depends(get_tenant_context),
) -> AuditVerifyResult:
    """Verify SHA-256 hash-chain integrity across the entire audit log."""
    from services.mcp_erp.tools import verify_all_audit_entries

    return verify_all_audit_entries()


@app.get("/v1/traces")
async def list_traces_endpoint(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    ctx: TenantContext = Depends(get_tenant_context),
) -> dict[str, Any]:
    """List recent gateway execution traces scoped to the caller's tenant."""
    await _ensure_services(request.app)
    trace_recorder: TraceRecorder = request.app.state.trace_recorder
    all_recent = trace_recorder.list_traces(limit=max(limit * 3, 50))
    if ctx.role == Role.ADMIN:
        scoped = all_recent[:limit]
    else:
        scoped = [t for t in all_recent if t.get("tenant_id") == ctx.tenant_id][:limit]
    return {"tenant_id": ctx.tenant_id, "traces": scoped}


@app.get("/v1/evals/latest")
async def latest_eval_report_endpoint() -> Any:
    """Return latest evaluation harness report JSON from evals/reports/."""
    reports_dir = PROJECT_ROOT / "evals" / "reports"
    if not reports_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No evaluation reports directory found.",
        )

    latest_path = reports_dir / "latest.json"
    if not latest_path.exists():
        candidates = sorted(
            (p for p in reports_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No evaluation reports found.",
            )
        latest_path = candidates[0]

    try:
        return json.loads(latest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to read evaluation report: {exc}",
        ) from exc
