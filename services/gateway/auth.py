"""Tenant authentication, RBAC guards, and role-based tool filtering."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, Header, HTTPException, status

from contracts.config import get_settings
from contracts.models import Role, TenantContext


class TenantRegistry:
    """Loads API key -> TenantContext mappings from tenants.json."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        settings = get_settings()
        self.config_path = Path(config_path or settings.tenants_config_path)
        self._keys: dict[str, TenantContext] = {}
        self.reload()

    def reload(self) -> None:
        """Reload tenant API key mappings from disk."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Tenants config not found: {self.config_path}")
        raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        api_keys = raw.get("api_keys", {})
        parsed: dict[str, TenantContext] = {}
        for api_key, info in api_keys.items():
            parsed[api_key] = TenantContext(
                tenant_id=str(info["tenant_id"]),
                user_id=str(info["user_id"]),
                role=Role(info["role"]),
                scopes=[str(s) for s in info.get("scopes", [])],
                rpm_limit=int(info.get("rpm_limit", 60)),
            )
        self._keys = parsed

    def resolve_key(self, api_key: str | None) -> TenantContext:
        """Resolve a raw API key into a TenantContext or raise HTTP 401."""
        if not api_key or not api_key.strip():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing authentication credentials (Bearer token or X-API-Key required).",
                headers={"WWW-Authenticate": "Bearer"},
            )
        ctx = self._keys.get(api_key.strip())
        if ctx is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return ctx


@lru_cache(maxsize=1)
def get_tenant_registry() -> TenantRegistry:
    """Return cached default TenantRegistry instance."""
    return TenantRegistry()


def extract_api_key(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract credential from Authorization: Bearer <key> or X-API-Key header."""
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
    if x_api_key:
        return x_api_key.strip()
    return None


async def get_tenant_context(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TenantContext:
    """FastAPI dependency resolving caller credentials to TenantContext."""
    api_key = extract_api_key(authorization, x_api_key)
    return get_tenant_registry().resolve_key(api_key)


def require_roles(*roles: Role | str) -> Callable[..., TenantContext]:
    """FastAPI dependency factory enforcing that caller holds one of the given roles."""
    allowed = {r.value if isinstance(r, Role) else str(r) for r in roles}

    def _checker(ctx: TenantContext = Depends(get_tenant_context)) -> TenantContext:
        if ctx.role.value not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Role '{ctx.role.value}' is not permitted; requires one of {sorted(allowed)}.",
            )
        return ctx

    return _checker


def _extract_tool_name(tool: dict[str, Any]) -> str:
    """Extract canonical tool name from OpenAI/MCP tool schema dict."""
    fn = tool.get("function")
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn["name"])
    return str(tool.get("name", ""))


def filter_tools_for_role(
    role: Role | str,
    tools: list[dict[str, Any]],
    role_tools_map: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Filter MCP/LLM tool schemas to only those authorized for the given role."""
    role_key = role.value if isinstance(role, Role) else str(role)
    allowed_list = role_tools_map.get(role_key, [])
    if "*" in allowed_list:
        return list(tools)
    allowed_set = set(allowed_list)
    return [tool for tool in tools if _extract_tool_name(tool) in allowed_set]
