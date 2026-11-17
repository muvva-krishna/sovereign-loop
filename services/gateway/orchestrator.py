"""Multi-round agent orchestrator enforcing policy, idempotency, context caps, PII scrubbing, and audit."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from loguru import logger

from contracts.models import (
    ChatRequest,
    ChatResponse,
    PiiRedactionSummary,
    PolicyDecision,
    PolicyOutcome,
    TenantContext,
    ToolCallTrace,
)
from services.gateway.auth import filter_tools_for_role
from services.gateway.llm_router import LLMRouter
from services.gateway.mcp_client import MCPClientManager
from services.gateway.observability import LangfuseTracer, TraceRecorder
from services.gateway.pii_scrubber import scrub_payload, scrub_text
from services.gateway.policy_engine import PolicyEngine

_MAX_LIST_ITEMS = 20
_MAX_STRING_CHARS = 4000
_MAX_PREVIEW_CHARS = 300
_WRITE_TOOL_PREFIXES = ("create_", "send_", "update_", "delete_")


def _server_from_tool_name(tool_name: str) -> str:
    if "." in tool_name:
        return tool_name.split(".", 1)[0]
    return "unknown"


def _is_write_tool(tool_name: str) -> bool:
    short = tool_name.split(".", 1)[-1]
    return short.startswith(_WRITE_TOOL_PREFIXES)


def compute_idempotency_key(
    seed: str,
    tenant_id: str,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Compute deterministic SHA-256 idempotency key from seed + tenant + tool + canonical business args."""
    business_args = {
        k: v for k, v in arguments.items() if k not in {"trace_id", "idempotency_key"}
    }
    canonical = json.dumps(business_args, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    raw = f"{seed}:{tenant_id}:{tool_name}:{canonical}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def optimize_context_output(raw_output: str) -> Any:
    """Parse tool output and cap oversized lists or strings with truncation metadata."""
    try:
        parsed = json.loads(raw_output)
    except json.JSONDecodeError:
        if len(raw_output) > _MAX_STRING_CHARS:
            return raw_output[:_MAX_STRING_CHARS] + f"... [truncated from {len(raw_output)} chars]"
        return raw_output

    def _cap_node(node: Any) -> Any:
        if isinstance(node, str):
            if len(node) > _MAX_STRING_CHARS:
                return node[:_MAX_STRING_CHARS] + f"... [truncated from {len(node)} chars]"
            return node
        if isinstance(node, list):
            capped_items = [_cap_node(item) for item in node[:_MAX_LIST_ITEMS]]
            if len(node) > _MAX_LIST_ITEMS:
                return {
                    "items": capped_items,
                    "truncated": True,
                    "returned_count": len(capped_items),
                    "total_count": len(node),
                }
            return capped_items
        if isinstance(node, dict):
            return {
                k: _cap_node(v)
                for k, v in node.items()
                if str(k).lower() != "approval_token"
            }
        return node

    return _cap_node(parsed)


def _record_audit_safe(
    *,
    tenant_id: str,
    trace_id: str,
    actor_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    outcome: str,
    result_summary: str,
) -> None:
    """Persist hash-chained audit entry via services.mcp_erp.tools.record_audit_entry."""
    try:
        from services.mcp_erp.tools import record_audit_entry

        record_audit_entry(
            tenant_id=tenant_id,
            trace_id=trace_id,
            actor_id=actor_id,
            tool_name=tool_name,
            arguments_dict=arguments,
            outcome=outcome,
            result_summary=result_summary,
        )
    except Exception as exc:
        logger.warning(f"Audit log persistence skipped for {tool_name}: {exc}")


class Orchestrator:
    """Coordinates LLM tool planning, policy checks, MCP execution, PII scrubbing, and tracing."""

    def __init__(
        self,
        *,
        policy_engine: PolicyEngine,
        mcp_client: MCPClientManager,
        llm_router: LLMRouter,
        trace_recorder: TraceRecorder,
        langfuse_tracer: LangfuseTracer | None = None,
    ) -> None:
        self.policy_engine = policy_engine
        self.mcp_client = mcp_client
        self.llm_router = llm_router
        self.trace_recorder = trace_recorder
        self.langfuse_tracer = langfuse_tracer

    def _build_system_prompt(self, ctx: TenantContext) -> str:
        return (
            "You are the Sovereign Enterprise Operations Assistant. "
            f"Authenticated tenant_id={ctx.tenant_id}, user_id={ctx.user_id}, role={ctx.role.value}. "
            "Use authorized MCP tools to retrieve factual enterprise data and follow all policies."
        )

    async def run_chat(self, ctx: TenantContext, request: ChatRequest) -> ChatResponse:
        """Execute multi-round tool-calling loop and return grounded ChatResponse."""
        t0 = time.perf_counter()
        trace_id = f"tr_{uuid.uuid4().hex[:16]}"

        all_tools = self.mcp_client.list_tools()
        allowed_tools = filter_tools_for_role(
            ctx.role, all_tools, self.policy_engine.role_tools_map
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self._build_system_prompt(ctx)},
            {"role": "user", "content": request.message},
        ]

        tool_traces: list[ToolCallTrace] = []
        pending_approvals: list[dict[str, Any]] = []
        total_tokens = 0
        total_cost = 0.0
        total_pii_redacted = 0
        model_used = request.model or "offline/planner-v1"
        fallback_triggered = False
        final_answer = ""

        for _round in range(request.max_rounds):
            llm_resp = await self.llm_router.generate(
                messages=messages,
                tools=allowed_tools,
                model=request.model,
            )
            model_used = llm_resp.model_used
            fallback_triggered = fallback_triggered or llm_resp.fallback_triggered
            total_tokens += llm_resp.tokens_used
            total_cost += llm_resp.cost_usd

            if not llm_resp.tool_calls:
                final_answer = llm_resp.content
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": llm_resp.content or "",
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": {
                                "name": tc["name"],
                                "arguments": json.dumps(tc.get("arguments", {})),
                            },
                        }
                        for tc in llm_resp.tool_calls
                    ],
                }
            )

            for tc in llm_resp.tool_calls:
                call_id = str(tc.get("id") or f"call_{uuid.uuid4().hex[:8]}")
                tool_name = str(tc.get("name", ""))
                raw_args = dict(tc.get("arguments") or {})

                # Inject caller tenant_id by default; preserve explicit foreign tenant_id so R-001 catches it.
                if "tenant_id" not in raw_args or raw_args["tenant_id"] is None or str(
                    raw_args["tenant_id"]
                ).strip() == "":
                    raw_args["tenant_id"] = ctx.tenant_id

                decision: PolicyDecision = self.policy_engine.evaluate(
                    ctx, tool_name, raw_args
                )
                step_t0 = time.perf_counter()

                if decision.outcome == PolicyOutcome.DENY:
                    duration_ms = round((time.perf_counter() - step_t0) * 1000.0, 2)
                    _record_audit_safe(
                        tenant_id=ctx.tenant_id,
                        trace_id=trace_id,
                        actor_id=ctx.user_id,
                        tool_name=tool_name,
                        arguments=raw_args,
                        outcome=PolicyOutcome.DENY.value,
                        result_summary=f"{decision.rule_id}: {decision.reason}",
                    )
                    tool_traces.append(
                        ToolCallTrace(
                            tool_name=tool_name,
                            server=_server_from_tool_name(tool_name),
                            arguments=raw_args,
                            policy_decision=decision,
                            duration_ms=duration_ms,
                            status="denied",
                            result_preview=decision.reason[:_MAX_PREVIEW_CHARS],
                            error=decision.reason,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": json.dumps(
                                {
                                    "policy_outcome": "DENY",
                                    "rule_id": decision.rule_id,
                                    "reason": decision.reason,
                                }
                            ),
                        }
                    )
                    continue

                # Compute deterministic idempotency key for REQUIRE_APPROVAL and write operations
                idem_key: str | None = None
                if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL or _is_write_tool(
                    tool_name
                ):
                    seed = request.idempotency_seed or trace_id
                    idem_key = compute_idempotency_key(
                        seed=seed,
                        tenant_id=ctx.tenant_id,
                        tool_name=tool_name,
                        arguments=raw_args,
                    )
                    raw_args.setdefault("idempotency_key", idem_key)

                if tool_name == "erp.create_requisition":
                    raw_args.setdefault("requested_by", ctx.user_id)
                    raw_args["trace_id"] = trace_id

                try:
                    raw_result = await self.mcp_client.call_tool(tool_name, raw_args)
                    optimized = optimize_context_output(raw_result)
                    scrubbed_obj, redaction_summary = scrub_payload(optimized)
                    total_pii_redacted += redaction_summary.redacted_count

                    serialized_scrubbed = (
                        scrubbed_obj
                        if isinstance(scrubbed_obj, str)
                        else json.dumps(scrubbed_obj, default=str)
                    )
                    cached_idempotent = bool(
                        isinstance(scrubbed_obj, dict)
                        and scrubbed_obj.get("cached_idempotent")
                    )

                    status_str = (
                        "pending_approval"
                        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL
                        else "success"
                    )
                    if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL and isinstance(
                        scrubbed_obj, dict
                    ):
                        pending_approvals.append(
                            {
                                "requisition_id": scrubbed_obj.get("requisition_id"),
                                "tenant_id": scrubbed_obj.get("tenant_id", ctx.tenant_id),
                                "material_id": scrubbed_obj.get("material_id"),
                                "quantity": scrubbed_obj.get("quantity"),
                                "status": scrubbed_obj.get("status", "pending_approval"),
                                "cached_idempotent": cached_idempotent,
                            }
                        )

                    duration_ms = round((time.perf_counter() - step_t0) * 1000.0, 2)
                    _record_audit_safe(
                        tenant_id=ctx.tenant_id,
                        trace_id=trace_id,
                        actor_id=ctx.user_id,
                        tool_name=tool_name,
                        arguments=raw_args,
                        outcome=decision.outcome.value,
                        result_summary=serialized_scrubbed[:500],
                    )
                    tool_traces.append(
                        ToolCallTrace(
                            tool_name=tool_name,
                            server=_server_from_tool_name(tool_name),
                            arguments=raw_args,
                            policy_decision=decision,
                            duration_ms=duration_ms,
                            redactions=redaction_summary,
                            idempotency_key=idem_key,
                            cached_idempotent=cached_idempotent,
                            status=status_str,
                            result_preview=serialized_scrubbed[:_MAX_PREVIEW_CHARS],
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": serialized_scrubbed,
                        }
                    )
                except Exception as exc:
                    duration_ms = round((time.perf_counter() - step_t0) * 1000.0, 2)
                    err_msg = f"{type(exc).__name__}: {exc}"
                    _record_audit_safe(
                        tenant_id=ctx.tenant_id,
                        trace_id=trace_id,
                        actor_id=ctx.user_id,
                        tool_name=tool_name,
                        arguments=raw_args,
                        outcome="ERROR",
                        result_summary=err_msg,
                    )
                    tool_traces.append(
                        ToolCallTrace(
                            tool_name=tool_name,
                            server=_server_from_tool_name(tool_name),
                            arguments=raw_args,
                            policy_decision=decision,
                            duration_ms=duration_ms,
                            redactions=PiiRedactionSummary(),
                            idempotency_key=idem_key,
                            status="error",
                            result_preview="",
                            error=err_msg,
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call_id,
                            "name": tool_name,
                            "content": json.dumps({"error": err_msg}),
                        }
                    )

        # If loop ended right after tool calls without a final assistant answer, force a synthesis turn.
        if not final_answer:
            synth_resp = await self.llm_router.generate(
                messages=messages,
                tools=[],
                model=request.model,
            )
            model_used = synth_resp.model_used
            fallback_triggered = fallback_triggered or synth_resp.fallback_triggered
            total_tokens += synth_resp.tokens_used
            total_cost += synth_resp.cost_usd
            final_answer = synth_resp.content or "Request processed."

        # Defense-in-depth PII scrub on final user-facing answer
        final_answer, answer_redactions = scrub_text(final_answer)
        total_pii_redacted += answer_redactions.redacted_count

        total_latency_ms = round((time.perf_counter() - t0) * 1000.0, 2)
        response = ChatResponse(
            trace_id=trace_id,
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            role=ctx.role,
            model_used=model_used,
            fallback_triggered=fallback_triggered,
            answer=final_answer,
            tool_traces=tool_traces,
            pending_approvals=pending_approvals,
            total_latency_ms=total_latency_ms,
            tokens_used=total_tokens,
            cost_usd=round(total_cost, 6),
            pii_redacted_count=total_pii_redacted,
        )

        trace_payload = {
            **response.model_dump(mode="json"),
            "prompt": request.message,
        }
        self.trace_recorder.record_trace(trace_payload)
        if self.langfuse_tracer is not None:
            self.langfuse_tracer.record_gateway_trace(trace_payload)

        return response
