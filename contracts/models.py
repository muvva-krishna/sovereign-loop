from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Role(str, Enum):
    VIEWER = "viewer"
    SALES_REP = "sales_rep"
    MANAGER = "manager"
    ADMIN = "admin"


class TenantContext(BaseModel):
    tenant_id: str
    user_id: str
    role: Role
    scopes: list[str] = Field(default_factory=list)
    rpm_limit: int = 60


class PolicyOutcome(str, Enum):
    ALLOW = "ALLOW"
    DENY = "DENY"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"


class PolicyDecision(BaseModel):
    outcome: PolicyOutcome
    rule_id: str
    reason: str
    matched_condition: str = ""


class PiiRedactionSummary(BaseModel):
    redacted_count: int = 0
    types_found: list[str] = Field(default_factory=list)


class ToolCallTrace(BaseModel):
    tool_name: str
    server: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    policy_decision: PolicyDecision
    duration_ms: float = 0.0
    redactions: PiiRedactionSummary = Field(default_factory=PiiRedactionSummary)
    idempotency_key: str | None = None
    cached_idempotent: bool = False
    status: str = "success"  # "success" | "pending_approval" | "denied" | "error"
    result_preview: str = ""
    error: str | None = None


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    model: str | None = None
    max_rounds: int = Field(default=5, ge=1, le=10)
    idempotency_seed: str | None = None


class ChatResponse(BaseModel):
    trace_id: str
    tenant_id: str
    user_id: str
    role: Role
    model_used: str
    fallback_triggered: bool = False
    answer: str
    tool_traces: list[ToolCallTrace] = Field(default_factory=list)
    pending_approvals: list[dict[str, Any]] = Field(default_factory=list)
    total_latency_ms: float = 0.0
    tokens_used: int = 0
    cost_usd: float = 0.0
    pii_redacted_count: int = 0


class AuditVerifyResult(BaseModel):
    valid: bool
    total_entries: int
    checked_entries: int
    broken_at_id: int | None = None
    reason: str = "OK"


class EvalCase(BaseModel):
    case_id: str
    category: str  # "analytics" | "knowledge_sql" | "approval_workflow" | "security_attack"
    tenant_api_key: str
    prompt: str
    expected_tools: list[str] = Field(default_factory=list)
    forbidden_tools: list[str] = Field(default_factory=list)
    expected_policy_outcome: PolicyOutcome | None = None
    must_contain: list[str] = Field(default_factory=list)
    must_not_contain: list[str] = Field(default_factory=list)
    expect_pending_approval: bool = False
    expect_pii_redacted: bool = False


class EvalCaseResult(BaseModel):
    case_id: str
    category: str
    model: str
    passed: bool
    tool_match: bool
    policy_match: bool
    answer_match: bool
    safety_clean: bool
    latency_ms: float
    tokens_used: int
    cost_usd: float
    failure_reasons: list[str] = Field(default_factory=list)


class ModelBaselineReport(BaseModel):
    model: str
    total_cases: int
    passed_cases: int
    pass_rate: float
    tool_accuracy: float
    policy_compliance_rate: float
    security_block_rate: float
    p50_latency_ms: float
    p95_latency_ms: float
    total_tokens: int
    total_cost_usd: float
    results: list[EvalCaseResult] = Field(default_factory=list)
