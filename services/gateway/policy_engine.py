"""Deterministic policy engine evaluating RBAC and safety guardrails before MCP calls."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from contracts.config import get_settings
from contracts.models import PolicyDecision, PolicyOutcome, TenantContext

DEFAULT_FORBIDDEN_TOOLS = frozenset(
    {"erp.approve_pending_requisition", "erp.reject_pending_requisition"}
)
DEFAULT_FORBIDDEN_PATTERNS = (
    "DROP TABLE",
    "DELETE FROM",
    "TRUNCATE ",
    "--",
    ";--",
)


class PolicyEngine:
    """Evaluates deterministic rules top-to-bottom from configs/policies.yaml."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        settings = get_settings()
        self.config_path = Path(config_path or settings.policies_config_path)
        self.role_tools_map: dict[str, list[str]] = {}
        self.rules: list[dict[str, Any]] = []
        self._rules_by_id: dict[str, dict[str, Any]] = {}
        self.reload()

    def reload(self) -> None:
        """Load policy YAML configuration from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Policies config not found: {self.config_path}")
        raw = yaml.safe_load(self.config_path.read_text(encoding="utf-8")) or {}
        role_tools = raw.get("role_tools", {})
        self.role_tools_map = {
            str(role): [str(t) for t in tools] for role, tools in role_tools.items()
        }
        self.rules = list(raw.get("rules", []))
        self._rules_by_id = {str(r["id"]): r for r in self.rules if "id" in r}

    def is_role_authorized(self, ctx: TenantContext, tool_name: str) -> bool:
        """Return True if caller's role is permitted to call tool_name."""
        allowed = self.role_tools_map.get(ctx.role.value, [])
        return "*" in allowed or tool_name in allowed

    def _rule_reason(self, rule_id: str, default: str) -> str:
        rule = self._rules_by_id.get(rule_id)
        if rule and rule.get("reason"):
            return str(rule["reason"])
        return default

    def evaluate(
        self,
        ctx: TenantContext,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """Evaluate all deterministic rules top-to-bottom for a tool invocation."""
        args = arguments or {}

        # R-001: Cross-tenant isolation check
        if "tenant_id" in args and args["tenant_id"] is not None:
            requested_tenant = str(args["tenant_id"]).strip()
            if requested_tenant and requested_tenant != ctx.tenant_id:
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    rule_id="R-001",
                    reason=self._rule_reason(
                        "R-001", "Cross-tenant access is strictly forbidden."
                    ),
                    matched_condition=f"tenant_id={requested_tenant} != {ctx.tenant_id}",
                )

        # R-002: Agents can never self-approve or reject requisitions
        r002_rule = self._rules_by_id.get("R-002", {})
        r002_tools = set(r002_rule.get("match", {}).get("tool_in", DEFAULT_FORBIDDEN_TOOLS))
        if tool_name in r002_tools:
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                rule_id="R-002",
                reason=self._rule_reason(
                    "R-002",
                    "Human-in-the-loop approvals must occur out-of-band via the authenticated Admin API.",
                ),
                matched_condition=f"tool={tool_name}",
            )

        # R-003: SQLi / destructive argument pattern guard
        r003_rule = self._rules_by_id.get("R-003", {})
        patterns = r003_rule.get("match", {}).get(
            "forbidden_patterns", list(DEFAULT_FORBIDDEN_PATTERNS)
        )
        serialized = json.dumps(args, sort_keys=True, default=str).upper()
        for pattern in patterns:
            pat_str = str(pattern)
            if pat_str.upper() in serialized:
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    rule_id="R-003",
                    reason=self._rule_reason(
                        "R-003", "Argument payload triggered deterministic injection guardrail."
                    ),
                    matched_condition=f"pattern={pat_str}",
                )

        # Role allowlist check (R-ROLE)
        if not self.is_role_authorized(ctx, tool_name):
            return PolicyDecision(
                outcome=PolicyOutcome.DENY,
                rule_id="R-ROLE",
                reason=f"Role '{ctx.role.value}' is not authorized to invoke '{tool_name}'.",
                matched_condition=f"role={ctx.role.value}, tool={tool_name}",
            )

        # R-101: Requisition hard quantity cap (> 10000)
        if tool_name == "erp.create_requisition":
            r101_rule = self._rules_by_id.get("R-101", {})
            threshold = int(
                r101_rule.get("match", {}).get("numeric_gt", {}).get("threshold", 10000)
            )
            raw_qty = args.get("quantity", 0)
            try:
                qty = int(raw_qty)
            except (TypeError, ValueError):
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    rule_id="R-101",
                    reason="Invalid requisition quantity; must be a positive integer.",
                    matched_condition=f"quantity={raw_qty!r}",
                )
            if qty > threshold:
                return PolicyDecision(
                    outcome=PolicyOutcome.DENY,
                    rule_id="R-101",
                    reason=self._rule_reason(
                        "R-101",
                        f"Single requisition quantity exceeds hard ceiling of {threshold:,} units.",
                    ),
                    matched_condition=f"quantity={qty} > {threshold}",
                )

            # R-102: Requisitions always require out-of-band human approval
            return PolicyDecision(
                outcome=PolicyOutcome.REQUIRE_APPROVAL,
                rule_id="R-102",
                reason=self._rule_reason(
                    "R-102",
                    "All inventory purchase requisitions require out-of-band human sign-off.",
                ),
                matched_condition="tool=erp.create_requisition",
            )

        # R-201: Authorized by role allowlist
        return PolicyDecision(
            outcome=PolicyOutcome.ALLOW,
            rule_id="R-201",
            reason=self._rule_reason(
                "R-201", "Authorized by role allowlist and deterministic policy checks."
            ),
            matched_condition=f"role={ctx.role.value}, tool={tool_name}",
        )
