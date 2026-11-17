"""Model-agnostic LLM router with LiteLLM retries/fallback and deterministic offline profiles."""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from typing import Any

from loguru import logger
from pydantic import BaseModel, Field

from contracts.config import get_settings
from services.gateway.vertex_adc import VertexADCClient, is_vertex_gemini_model

_KNOWN_CITIES = (
    "Bengaluru",
    "Bangalore",
    "Mumbai",
    "Hyderabad",
    "Delhi",
    "Chennai",
    "Pune",
    "Kolkata",
)
_KNOWN_TENANTS = ("havells_electricals", "licious_coldchain")
_SQLI_MARKERS = ("drop table", "delete from", "truncate ", ";--", "--")
_SKU_RE = re.compile(r"\b((?:HV|LC|HAV|LIC)-[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,8})?)\b")
_QTY_RE = re.compile(r"\b(\d{1,7})\s*(?:units?|qty|pcs|pieces|items?)\b", re.IGNORECASE)


class LLMResponse(BaseModel):
    """Normalized response returned by both LiteLLM and OfflineRouter."""

    content: str = ""
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    model_used: str
    fallback_triggered: bool = False
    tokens_used: int = 0
    cost_usd: float = 0.0


def _sanitize_tools_for_llm(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Strip internal MCP metadata fields before sending tool schemas to LLM APIs."""
    if not tools:
        return []
    cleaned: list[dict[str, Any]] = []
    for tool in tools:
        if "function" in tool:
            cleaned.append({"type": "function", "function": tool["function"]})
        else:
            cleaned.append(tool)
    return cleaned


def _extract_city(text: str) -> str | None:
    lower = text.lower()
    for city in _KNOWN_CITIES:
        if city.lower() in lower:
            return "Bengaluru" if city.lower() == "bangalore" else city
    return None


def _extract_sku(text: str) -> str | None:
    match = _SKU_RE.search(text.upper())
    return match.group(1) if match else None


def _extract_quantity(text: str) -> int | None:
    match = _QTY_RE.search(text)
    if match:
        return int(match.group(1))
    fallback = re.search(r"\bquantity\s*(?:of|:|=)?\s*(\d{1,7})\b", text, re.IGNORECASE)
    return int(fallback.group(1)) if fallback else None


def _extract_foreign_tenant(prompt: str, caller_tenant_id: str | None) -> str | None:
    lower = prompt.lower()
    for tenant in _KNOWN_TENANTS:
        if tenant in lower and caller_tenant_id and tenant != caller_tenant_id:
            return tenant
    if caller_tenant_id == "havells_electricals" and re.search(r"\btenant\s+licious\b", lower):
        return "licious_coldchain"
    if caller_tenant_id == "licious_coldchain" and re.search(r"\btenant\s+havells\b", lower):
        return "havells_electricals"
    return None


def _extract_sop_query(prompt: str) -> str:
    lower = prompt.lower()
    keywords: list[str] = []
    for term in (
        "restock",
        "threshold",
        "reorder",
        "safety stock",
        "credit limit",
        "warranty",
        "sla",
        "cold-chain",
        "cold chain",
        "temperature",
        "perishable",
        "escalation",
        "dispatch",
        "approval",
        "procurement",
    ):
        if term in lower:
            keywords.append(term)
    return " ".join(keywords) if keywords else prompt[:120]


def _extract_caller_tenant_from_messages(messages: list[dict[str, Any]]) -> str | None:
    for msg in messages:
        if msg.get("role") == "system":
            content = str(msg.get("content", ""))
            m = re.search(r"tenant_id=([a-zA-Z0-9_]+)", content)
            if m:
                return m.group(1)
    return None


def _split_current_turn(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    """Return (latest_user_prompt, tool_messages_after_last_user)."""
    last_user_idx = -1
    last_user_text = ""
    for idx, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = idx
            last_user_text = str(msg.get("content", ""))
    if last_user_idx == -1:
        return "", []
    tool_msgs = [
        m for m in messages[last_user_idx + 1 :] if m.get("role") == "tool"
    ]
    return last_user_text, tool_msgs


def _make_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f"call_{uuid.uuid4().hex[:10]}",
        "name": name,
        "arguments": arguments,
    }


def _synthesize_answer(
    prompt: str,
    tool_msgs: list[dict[str, Any]],
    *,
    verbose: bool = True,
) -> str:
    """Build a deterministic, grounded answer from tool outputs."""
    if not tool_msgs:
        return (
            "I could not find an applicable tool for that request. "
            "Please specify an ERP, SOP knowledge, or operations query."
        )

    denied_reasons: list[str] = []
    pending_items: list[str] = []
    findings: list[str] = []

    for msg in tool_msgs:
        tool_name = str(msg.get("name", "tool"))
        raw_content = str(msg.get("content", ""))
        try:
            parsed: Any = json.loads(raw_content)
        except json.JSONDecodeError:
            parsed = raw_content

        if isinstance(parsed, dict) and parsed.get("policy_outcome") == "DENY":
            denied_reasons.append(
                f"{tool_name} denied ({parsed.get('rule_id', 'POLICY')}): "
                f"{parsed.get('reason', 'Blocked by policy')}"
            )
            continue

        if isinstance(parsed, dict) and parsed.get("status") == "pending_approval":
            req_id = parsed.get("requisition_id", "pending")
            mat_id = parsed.get("material_id", "")
            qty = parsed.get("quantity", "")
            pending_items.append(
                f"Requisition {req_id} for {qty} units of {mat_id} is pending human approval."
            )
            continue

        if isinstance(parsed, dict):
            summary_bits = [
                f"{k}={v}"
                for k, v in parsed.items()
                if k not in {"breakdown", "results", "chunks", "items"}
            ]
            if "results" in parsed and isinstance(parsed["results"], list):
                snippets = [
                    str(r.get("title") or r.get("doc_id") or r.get("snippet") or r)[:140]
                    for r in parsed["results"][:3]
                    if isinstance(r, dict)
                ]
                findings.append(f"{tool_name}: " + "; ".join(snippets or [json.dumps(parsed)[:200]]))
            elif "items" in parsed and isinstance(parsed["items"], list):
                findings.append(
                    f"{tool_name} ({len(parsed['items'])} items): "
                    + json.dumps(parsed["items"][:5], default=str)[:320]
                )
            else:
                findings.append(f"{tool_name}: " + ", ".join(summary_bits)[:320])
        elif isinstance(parsed, list):
            item_summaries: list[str] = []
            for entry in parsed[:5]:
                if not isinstance(entry, dict):
                    item_summaries.append(str(entry))
                elif "doc_id" in entry:
                    sec = f" [{entry['section']}]" if entry.get("section") else ""
                    item_summaries.append(f"**{entry['doc_id']}**{sec}: {entry.get('title', '')}")
                elif "order_id" in entry:
                    item_summaries.append(
                        f"**{entry['order_id']}** ({entry.get('city', '')}, "
                        f"{entry.get('status', '')}, ₹{entry.get('total_value', '')})"
                    )
                elif "material_id" in entry:
                    city_part = f" {entry['city']}" if entry.get("city") else ""
                    item_summaries.append(
                        f"**{entry['material_id']}** ({city_part.strip() or 'HQ'}, "
                        f"qty={entry.get('quantity_on_hand')}/{entry.get('reorder_point')})"
                    )
                else:
                    item_summaries.append(json.dumps(entry, default=str)[:90])
            suffix = f" (+{len(parsed) - 5} more)" if len(parsed) > 5 else ""
            findings.append(f"{tool_name} ({len(parsed)} records): " + "; ".join(item_summaries) + suffix)
        else:
            findings.append(f"{tool_name}: {str(parsed)[:320]}")

    if denied_reasons and not findings and not pending_items:
        return "Request blocked by policy guardrails: " + " | ".join(denied_reasons)

    parts: list[str] = []
    if pending_items:
        parts.extend(pending_items)
    if findings:
        if verbose:
            parts.append("Here are the verified results:\n- " + "\n- ".join(findings))
        else:
            parts.append("Summary: " + "; ".join(findings[:1]))
    if denied_reasons:
        parts.append("Policy notices: " + " | ".join(denied_reasons))
    return "\n".join(parts) if parts else f"Completed request: {prompt[:80]}"


class OfflineRouter:
    """Deterministic rule-guided router supporting two distinct offline profiles."""

    PROFILE_PLANNER = "offline/planner-v1"
    PROFILE_BASELINE = "offline/baseline-lite"

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        profile = (
            self.PROFILE_BASELINE
            if model == self.PROFILE_BASELINE
            else self.PROFILE_PLANNER
        )
        prompt, tool_msgs = _split_current_turn(messages)
        caller_tenant = _extract_caller_tenant_from_messages(messages)
        available = {
            str(t.get("function", {}).get("name") or t.get("name", ""))
            for t in (tools or [])
        }

        if profile == self.PROFILE_BASELINE:
            return self._generate_baseline(prompt, tool_msgs, caller_tenant, available)
        return self._generate_planner(prompt, tool_msgs, caller_tenant, available)

    def _generate_planner(
        self,
        prompt: str,
        tool_msgs: list[dict[str, Any]],
        caller_tenant: str | None,
        available: set[str],
    ) -> LLMResponse:
        lower = prompt.lower()
        called_tools = {str(m.get("name", "")) for m in tool_msgs}
        sku = _extract_sku(prompt)
        city = _extract_city(prompt)
        qty = _extract_quantity(prompt)
        foreign_tenant = _extract_foreign_tenant(prompt, caller_tenant)

        # Round 2+: check if a secondary SOP lookup is needed before synthesizing
        if tool_msgs:
            wants_sop = any(
                k in lower for k in ("sop", "policy", "sla", "threshold", "guideline", "rules")
            )
            any_denied = any(
                '"policy_outcome": "DENY"' in str(m.get("content", ""))
                or '"DENY"' in str(m.get("content", ""))
                for m in tool_msgs
            )
            if (
                wants_sop
                and not any_denied
                and "knowledge.search_sops" in available
                and "knowledge.search_sops" not in called_tools
            ):
                return LLMResponse(
                    content="",
                    tool_calls=[
                        _make_call(
                            "knowledge.search_sops",
                            {"query": _extract_sop_query(prompt), "top_k": 3},
                        )
                    ],
                    model_used=self.PROFILE_PLANNER,
                    tokens_used=120,
                )
            return LLMResponse(
                content=_synthesize_answer(prompt, tool_msgs, verbose=True),
                tool_calls=[],
                model_used=self.PROFILE_PLANNER,
                tokens_used=180,
            )

        # Round 1: Security guardrail triggers (always emit so PolicyEngine enforces & audits)
        if "approve_pending_requisition" in lower or re.search(
            r"\b(?:auto-?approve|approve\s+(?:pending\s+)?requisition)\b", lower
        ):
            return LLMResponse(
                tool_calls=[_make_call("erp.approve_pending_requisition", {"requisition_id": "1"})],
                model_used=self.PROFILE_PLANNER,
                tokens_used=95,
            )
        if "reject_pending_requisition" in lower or re.search(
            r"\breject\s+(?:pending\s+)?requisition\b", lower
        ):
            return LLMResponse(
                tool_calls=[_make_call("erp.reject_pending_requisition", {"requisition_id": "1"})],
                model_used=self.PROFILE_PLANNER,
                tokens_used=95,
            )

        if any(marker in lower for marker in _SQLI_MARKERS):
            if "low stock" in lower:
                return LLMResponse(
                    tool_calls=[_make_call("erp.get_low_stock_items", {"city": prompt})],
                    model_used=self.PROFILE_PLANNER,
                    tokens_used=100,
                )
            return LLMResponse(
                tool_calls=[_make_call("erp.check_inventory", {"material_id": prompt})],
                model_used=self.PROFILE_PLANNER,
                tokens_used=100,
            )

        if foreign_tenant:
            if "inventory" in lower or sku:
                args: dict[str, Any] = {"tenant_id": foreign_tenant}
                if sku:
                    args["material_id"] = sku
                return LLMResponse(
                    tool_calls=[_make_call("erp.check_inventory", args)],
                    model_used=self.PROFILE_PLANNER,
                    tokens_used=105,
                )
            return LLMResponse(
                tool_calls=[_make_call("erp.get_open_orders", {"tenant_id": foreign_tenant})],
                model_used=self.PROFILE_PLANNER,
                tokens_used=105,
            )

        # Purchase requisition (emit even if caller role lacks permission so PolicyEngine audits DENY)
        if "requisition" in lower or "replenish" in lower:
            req_args: dict[str, Any] = {
                "material_id": sku or "HV-FAN-001",
                "quantity": qty or 100,
            }
            if city:
                req_args["city"] = city
            return LLMResponse(
                tool_calls=[_make_call("erp.create_requisition", req_args)],
                model_used=self.PROFILE_PLANNER,
                tokens_used=115,
            )

        # Escalation tickets / ops alerts
        if ("ticket" in lower or "escalat" in lower) and any(
            w in lower for w in ("create", "raise", "open", "file", "log")
        ) and "sop" not in lower:
            severity = "high" if any(w in lower for w in ("high", "critical", "alert")) else "medium"
            return LLMResponse(
                tool_calls=[
                    _make_call(
                        "ops.create_escalation_ticket",
                        {
                            "title": prompt[:120],
                            "severity": severity,
                            "related_id": sku or city or "",
                        },
                    )
                ],
                model_used=self.PROFILE_PLANNER,
                tokens_used=110,
            )

        if "list" in lower and "ticket" in lower and "ops.list_tickets" in available:
            return LLMResponse(
                tool_calls=[_make_call("ops.list_tickets", {})],
                model_used=self.PROFILE_PLANNER,
                tokens_used=95,
            )

        if "send" in lower and "alert" in lower and "ops.send_ops_alert" in available:
            return LLMResponse(
                tool_calls=[
                    _make_call("ops.send_ops_alert", {"title": prompt[:120], "severity": "high"})
                ],
                model_used=self.PROFILE_PLANNER,
                tokens_used=105,
            )

        # Distributor profile lookup
        if ("distributor" in lower or "partner" in lower) and "profile" in lower:
            dist_args: dict[str, Any] = {}
            if city:
                dist_args["city"] = city
            return LLMResponse(
                tool_calls=[_make_call("erp.get_distributor_profile", dist_args)],
                model_used=self.PROFILE_PLANNER,
                tokens_used=105,
            )

        # Analytics / multi-tool planning
        planned: list[dict[str, Any]] = []
        if ("low stock" in lower or "low on stock" in lower or "low in stock" in lower) and (
            "erp.get_low_stock_items" in available
        ):
            low_args: dict[str, Any] = {}
            if city:
                low_args["city"] = city
            planned.append(_make_call("erp.get_low_stock_items", low_args))

        if (
            ("inventory" in lower or ("stock" in lower and "low" not in lower) or sku)
            and "erp.check_inventory" in available
            and (sku is not None or not any(c["name"] == "erp.get_low_stock_items" for c in planned))
        ):
            inv_args: dict[str, Any] = {}
            if sku:
                inv_args["material_id"] = sku
            if city and not sku:
                inv_args["city"] = city
            planned.append(_make_call("erp.check_inventory", inv_args))

        if (
            "open order" in lower
            or "open cold-chain order" in lower
            or "pending fulfillment" in lower
        ) and "erp.get_open_orders" in available:
            ord_args: dict[str, Any] = {}
            if city:
                ord_args["city"] = city
            planned.append(_make_call("erp.get_open_orders", ord_args))

        if any(
            kw in lower
            for kw in ("sales summary", "city sales", "summarize sales", "summarize city sales", "revenue")
        ) and "erp.get_sales_summary" in available:
            planned.append(_make_call("erp.get_sales_summary", {"group_by": "city"}))

        if planned:
            return LLMResponse(
                tool_calls=planned,
                model_used=self.PROFILE_PLANNER,
                tokens_used=110 + 15 * len(planned),
            )

        # Pure SOP/knowledge search fallback
        if (
            any(k in lower for k in ("sop", "policy", "sla", "document", "guideline"))
            and "knowledge.search_sops" in available
        ):
            return LLMResponse(
                tool_calls=[
                    _make_call(
                        "knowledge.search_sops",
                        {"query": _extract_sop_query(prompt), "top_k": 3},
                    )
                ],
                model_used=self.PROFILE_PLANNER,
                tokens_used=100,
            )

        return LLMResponse(
            content="Please provide a specific SKU, order, distributor, SOP, or operations query.",
            tool_calls=[],
            model_used=self.PROFILE_PLANNER,
            tokens_used=60,
        )

    def _generate_baseline(
        self,
        prompt: str,
        tool_msgs: list[dict[str, Any]],
        caller_tenant: str | None,
        available: set[str],
    ) -> LLMResponse:
        """Weaker single-round baseline model profile for comparative eval benchmarking."""
        lower = prompt.lower()
        if tool_msgs:
            # Deliberately terse single-tool synthesis without secondary SOP lookup
            return LLMResponse(
                content=_synthesize_answer(prompt, tool_msgs, verbose=False),
                tool_calls=[],
                model_used=self.PROFILE_BASELINE,
                tokens_used=140,
            )

        sku = _extract_sku(prompt)
        qty = _extract_quantity(prompt)
        foreign_tenant = _extract_foreign_tenant(prompt, caller_tenant)

        if "approve_pending_requisition" in lower or "approve" in lower and "requisition" in lower:
            return LLMResponse(
                tool_calls=[_make_call("erp.approve_pending_requisition", {"requisition_id": "1"})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=85,
            )
        if any(marker in lower for marker in _SQLI_MARKERS):
            return LLMResponse(
                tool_calls=[_make_call("erp.check_inventory", {"material_id": prompt})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=85,
            )
        if foreign_tenant:
            return LLMResponse(
                tool_calls=[_make_call("erp.get_open_orders", {"tenant_id": foreign_tenant})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=85,
            )
        if "requisition" in lower:
            return LLMResponse(
                tool_calls=[
                    _make_call(
                        "erp.create_requisition",
                        {"material_id": sku or "HV-FAN-001", "quantity": qty or 100},
                    )
                ],
                model_used=self.PROFILE_BASELINE,
                tokens_used=95,
            )
        if ("ticket" in lower or "escalat" in lower) and "ops.create_escalation_ticket" in available:
            return LLMResponse(
                tool_calls=[
                    _make_call("ops.create_escalation_ticket", {"title": prompt[:80], "severity": "medium"})
                ],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        if "profile" in lower and "erp.get_distributor_profile" in available:
            return LLMResponse(
                tool_calls=[_make_call("erp.get_distributor_profile", {})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        # Suboptimal single-tool picks (omits city filters and secondary tools)
        if "low" in lower and "erp.get_low_stock_items" in available:
            return LLMResponse(
                tool_calls=[_make_call("erp.get_low_stock_items", {})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        if ("inventory" in lower or "stock" in lower or sku) and "erp.check_inventory" in available:
            return LLMResponse(
                tool_calls=[_make_call("erp.check_inventory", {"material_id": sku} if sku else {})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        if "sales" in lower and "erp.get_sales_summary" in available:
            return LLMResponse(
                tool_calls=[_make_call("erp.get_sales_summary", {})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        if "order" in lower and "erp.get_open_orders" in available:
            return LLMResponse(
                tool_calls=[_make_call("erp.get_open_orders", {})],
                model_used=self.PROFILE_BASELINE,
                tokens_used=90,
            )
        return LLMResponse(
            content="Unable to complete request without more specific instructions.",
            tool_calls=[],
            model_used=self.PROFILE_BASELINE,
            tokens_used=55,
        )


class LLMRouter:
    """Routes chat completions to Vertex AI (via ADC), LiteLLM, or OfflineRouter."""

    def __init__(self) -> None:
        self.offline = OfflineRouter()
        self.vertex = VertexADCClient()

    @staticmethod
    def _parse_litellm_response(
        raw_response: Any,
        model_used: str,
        fallback_triggered: bool,
    ) -> LLMResponse:
        choice = raw_response.choices[0]
        message = choice.message
        content = str(getattr(message, "content", None) or "")

        parsed_calls: list[dict[str, Any]] = []
        raw_calls = getattr(message, "tool_calls", None) or []
        for tc in raw_calls:
            fn = getattr(tc, "function", None)
            if fn is None and isinstance(tc, dict):
                fn = tc.get("function", {})
            name = getattr(fn, "name", None) or (fn.get("name") if isinstance(fn, dict) else "")
            raw_args = getattr(fn, "arguments", None) or (
                fn.get("arguments") if isinstance(fn, dict) else "{}"
            )
            if isinstance(raw_args, str):
                try:
                    args = json.loads(raw_args) if raw_args.strip() else {}
                except json.JSONDecodeError:
                    args = {"raw": raw_args}
            elif isinstance(raw_args, dict):
                args = raw_args
            else:
                args = {}
            call_id = getattr(tc, "id", None) or (
                tc.get("id") if isinstance(tc, dict) else f"call_{uuid.uuid4().hex[:10]}"
            )
            parsed_calls.append({"id": str(call_id), "name": str(name), "arguments": args})

        usage = getattr(raw_response, "usage", None)
        tokens_used = int(getattr(usage, "total_tokens", 0) or 0)

        cost_usd = 0.0
        try:
            import litellm

            cost_usd = float(litellm.completion_cost(completion_response=raw_response) or 0.0)
        except Exception:
            cost_usd = 0.0

        return LLMResponse(
            content=content,
            tool_calls=parsed_calls,
            model_used=model_used,
            fallback_triggered=fallback_triggered,
            tokens_used=tokens_used,
            cost_usd=round(cost_usd, 6),
        )

    async def _call_litellm_with_retries(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        timeout: float,
        max_retries: int,
        ollama_api_base: str,
    ) -> Any:
        import litellm

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "timeout": timeout,
                }
                if tools:
                    kwargs["tools"] = tools
                if model.startswith("ollama/"):
                    kwargs["api_base"] = ollama_api_base
                return await litellm.acompletion(**kwargs)
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = min(4.0, 0.5 * (2**attempt))
                    logger.warning(
                        f"LiteLLM call to '{model}' failed (attempt {attempt + 1}): {exc}; "
                        f"retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
        assert last_exc is not None
        raise last_exc

    async def _invoke_remote_model(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        sanitized_tools: list[dict[str, Any]],
        timeout: float,
        max_retries: int,
        ollama_api_base: str,
        fallback_triggered: bool,
    ) -> LLMResponse:
        if is_vertex_gemini_model(model):
            res = await self.vertex.generate(
                model=model,
                messages=messages,
                tools=sanitized_tools,
                timeout=timeout,
                max_retries=max_retries,
            )
            return LLMResponse(
                content=str(res.get("content") or ""),
                tool_calls=list(res.get("tool_calls") or []),
                model_used=str(res.get("model_used") or model),
                fallback_triggered=fallback_triggered,
                tokens_used=int(res.get("tokens_used") or 0),
                cost_usd=float(res.get("cost_usd") or 0.0),
            )

        raw = await self._call_litellm_with_retries(
            model=model,
            messages=messages,
            tools=sanitized_tools,
            timeout=timeout,
            max_retries=max_retries,
            ollama_api_base=ollama_api_base,
        )
        return self._parse_litellm_response(
            raw, model_used=model, fallback_triggered=fallback_triggered
        )

    async def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Generate assistant response or tool calls using OfflineRouter, Vertex ADC, or LiteLLM."""
        settings = get_settings()
        requested_model = (model or "").strip() or None
        llm_mode = settings.llm_mode.strip().lower()

        if requested_model and requested_model.startswith("offline/"):
            return self.offline.generate(messages=messages, tools=tools, model=requested_model)
        if not requested_model and llm_mode == "offline":
            return self.offline.generate(messages=messages, tools=tools, model=None)

        primary = requested_model or settings.primary_model
        fallback = settings.fallback_model
        sanitized_tools = _sanitize_tools_for_llm(tools)

        try:
            return await self._invoke_remote_model(
                model=primary,
                messages=messages,
                sanitized_tools=sanitized_tools,
                timeout=settings.llm_timeout_seconds,
                max_retries=settings.llm_max_retries,
                ollama_api_base=settings.ollama_api_base,
                fallback_triggered=False,
            )
        except Exception as primary_exc:
            logger.error(
                f"Primary model '{primary}' failed after retries ({primary_exc}); "
                f"falling back to '{fallback}'."
            )
            return await self._invoke_remote_model(
                model=fallback,
                messages=messages,
                sanitized_tools=sanitized_tools,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,
                ollama_api_base=settings.ollama_api_base,
                fallback_triggered=True,
            )
