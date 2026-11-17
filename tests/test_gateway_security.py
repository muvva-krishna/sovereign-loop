"""Unit tests for gateway security layers: PolicyEngine, PII scrubber, RateLimiter, and LLMRouter."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from contracts.models import PolicyOutcome, Role, TenantContext
from services.gateway.llm_router import LLMRouter
from services.gateway.pii_scrubber import scrub_payload, scrub_text
from services.gateway.policy_engine import PolicyEngine
from services.gateway.rate_limiter import RateLimiter


def _ctx(role: Role, tenant_id: str = "havells_electricals") -> TenantContext:
    return TenantContext(
        tenant_id=tenant_id,
        user_id=f"user-{role.value}",
        role=role,
        scopes=[],
        rpm_limit=60,
    )


def test_policy_engine_rules() -> None:
    engine = PolicyEngine()

    # R-001: Cross-tenant access blocked
    d_tenant = engine.evaluate(
        _ctx(Role.ADMIN, "havells_electricals"),
        "erp.get_open_orders",
        {"tenant_id": "licious_coldchain"},
    )
    assert d_tenant.outcome == PolicyOutcome.DENY
    assert d_tenant.rule_id == "R-001"

    # R-002: Agent cannot invoke human approval tools directly
    d_self_approve = engine.evaluate(
        _ctx(Role.ADMIN),
        "erp.approve_pending_requisition",
        {"requisition_id": "PR-123"},
    )
    assert d_self_approve.outcome == PolicyOutcome.DENY
    assert d_self_approve.rule_id == "R-002"

    # R-003: SQLi destructive patterns blocked
    d_sqli = engine.evaluate(
        _ctx(Role.SALES_REP),
        "erp.get_open_orders",
        {"city": "Mumbai'; DROP TABLE sales_orders;--"},
    )
    assert d_sqli.outcome == PolicyOutcome.DENY
    assert d_sqli.rule_id == "R-003"

    # R-ROLE: Viewer cannot invoke write tool
    d_role = engine.evaluate(
        _ctx(Role.VIEWER),
        "erp.create_requisition",
        {"material_id": "HV-FAN-001", "quantity": 10},
    )
    assert d_role.outcome == PolicyOutcome.DENY
    assert d_role.rule_id == "R-ROLE"

    # R-101: Requisition ceiling (> 10,000 units)
    d_ceiling = engine.evaluate(
        _ctx(Role.MANAGER),
        "erp.create_requisition",
        {"material_id": "HV-FAN-001", "quantity": 15000},
    )
    assert d_ceiling.outcome == PolicyOutcome.DENY
    assert d_ceiling.rule_id == "R-101"

    # R-102: Valid requisition pauses for human approval
    d_req_approval = engine.evaluate(
        _ctx(Role.MANAGER),
        "erp.create_requisition",
        {"material_id": "HV-FAN-001", "quantity": 250},
    )
    assert d_req_approval.outcome == PolicyOutcome.REQUIRE_APPROVAL
    assert d_req_approval.rule_id == "R-102"

    # R-201: Authorized read tool allowed
    d_allow = engine.evaluate(
        _ctx(Role.VIEWER),
        "erp.check_inventory",
        {"material_id": "HV-FAN-001"},
    )
    assert d_allow.outcome == PolicyOutcome.ALLOW
    assert d_allow.rule_id == "R-201"


def test_pii_scrubber_detects_and_redacts_all_indian_and_secret_types() -> None:
    sample = (
        "Contact Priya at priya.sharma@distributor.in or +91 98765 43210. "
        "GSTIN 29ABCDE1234F1Z5, PAN ABCDE1234F, Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.token"
    )
    cleaned, summary = scrub_text(sample)
    assert "priya.sharma@distributor.in" not in cleaned
    assert "98765" not in cleaned
    assert "29ABCDE1234F1Z5" not in cleaned
    assert "ABCDE1234F" not in cleaned
    assert "eyJhbGciOiJIUzI1NiJ9" not in cleaned
    assert "[REDACTED:EMAIL]" in cleaned
    assert "[REDACTED:PHONE]" in cleaned
    assert "[REDACTED:GSTIN]" in cleaned
    assert "[REDACTED:PAN]" in cleaned
    assert "[REDACTED:TOKEN]" in cleaned
    assert summary.redacted_count >= 5

    # Recursive payload scrubbing skips false positives
    payload = {
        "order_id": "HV-ORD-100001",
        "email": "ops@havells-dist.in",
        "nested": ["GSTIN 27AAACH1111A1Z2", "plain SKU HV-FAN-001"],
    }
    scrubbed_obj, payload_summary = scrub_payload(payload)
    assert scrubbed_obj["email"] == "[REDACTED:EMAIL]"
    assert "[REDACTED:GSTIN]" in scrubbed_obj["nested"][0]
    assert scrubbed_obj["nested"][1] == "plain SKU HV-FAN-001"
    assert payload_summary.redacted_count == 2


async def test_rate_limiter_sliding_window_enforces_limit() -> None:
    limiter = RateLimiter(redis_url="", window_seconds=60)
    r1 = await limiter.check_and_consume("tenant:user1", limit=2)
    r2 = await limiter.check_and_consume("tenant:user1", limit=2)
    r3 = await limiter.check_and_consume("tenant:user1", limit=2)
    other = await limiter.check_and_consume("tenant:user2", limit=2)

    assert r1.allowed is True and r1.remaining == 1
    assert r2.allowed is True and r2.remaining == 0
    assert r3.allowed is False and r3.retry_after_seconds > 0
    assert other.allowed is True


async def test_llm_router_fallback_when_primary_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "litellm")
    monkeypatch.setenv("PRIMARY_MODEL", "openai/primary-test")
    monkeypatch.setenv("FALLBACK_MODEL", "openai/fallback-test")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    from contracts.config import get_settings

    get_settings.cache_clear()
    calls: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> Any:
        model = str(kwargs["model"])
        calls.append(model)
        if model == "openai/primary-test":
            raise RuntimeError("Primary upstream unavailable")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Fallback reply", tool_calls=[]))],
            usage=SimpleNamespace(total_tokens=42),
        )

    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    router = LLMRouter()
    resp = await router.generate(messages=[{"role": "user", "content": "Hello"}])

    assert resp.fallback_triggered is True
    assert resp.content == "Fallback reply"
    assert resp.tokens_used == 42
    assert len(calls) == 2

    for var in ("LLM_MODE", "PRIMARY_MODEL", "FALLBACK_MODEL", "LLM_MAX_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()


async def test_llm_router_vertex_adc_gemini_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODE", "vertex_adc")
    monkeypatch.setenv("PRIMARY_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("LLM_MAX_RETRIES", "0")
    from contracts.config import get_settings

    get_settings.cache_clear()
    seen_models: list[str] = []

    async def fake_vertex_generate(**kwargs: Any) -> dict[str, Any]:
        model = str(kwargs["model"])
        seen_models.append(model)
        if model == "gemini-3.8-flash":
            raise RuntimeError("Transient Vertex 503")
        return {
            "content": "Gemini Flash-Lite fallback response",
            "tool_calls": [],
            "model_used": model,
            "tokens_used": 64,
            "cost_usd": 0.00012,
        }

    router = LLMRouter()
    monkeypatch.setattr(router.vertex, "generate", fake_vertex_generate)
    resp = await router.generate(messages=[{"role": "user", "content": "Check Mumbai stock"}])

    assert resp.fallback_triggered is True
    assert resp.model_used == "gemini-3.5-flash-lite"
    assert resp.content == "Gemini Flash-Lite fallback response"
    assert seen_models == ["gemini-3.8-flash", "gemini-3.5-flash-lite"]

    for var in ("LLM_MODE", "PRIMARY_MODEL", "FALLBACK_MODEL", "LLM_MAX_RETRIES"):
        monkeypatch.delenv(var, raising=False)
    get_settings.cache_clear()
