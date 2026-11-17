"""MCP client manager supporting remote Streamable HTTP/SSE/stdio and in-process backends."""

from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import sys
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Callable

from loguru import logger
from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamablehttp_client
from pydantic import BaseModel

from contracts.config import PROJECT_ROOT, get_settings

INPROCESS_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "server": "erp",
        "original_name": "get_open_orders",
        "function": {
            "name": "erp.get_open_orders",
            "description": (
                "List open sales orders for the authenticated tenant, "
                "optionally filtered by status or city."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "status": {"type": "string", "description": "Order status filter (e.g. open, pending)"},
                    "city": {"type": "string", "description": "Optional city filter"},
                    "limit": {"type": "integer", "description": "Maximum rows to return", "default": 20},
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "erp",
        "original_name": "get_sales_summary",
        "function": {
            "name": "erp.get_sales_summary",
            "description": "Get aggregated sales order counts and revenue breakdown for the tenant.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "group_by": {
                        "type": "string",
                        "enum": ["city", "status"],
                        "default": "city",
                    },
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "erp",
        "original_name": "check_inventory",
        "function": {
            "name": "erp.check_inventory",
            "description": "Check current stock levels and reorder thresholds for a SKU or warehouse.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "material_id": {"type": "string", "description": "Material / SKU code"},
                    "city": {"type": "string", "description": "Optional city filter"},
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "erp",
        "original_name": "get_low_stock_items",
        "function": {
            "name": "erp.get_low_stock_items",
            "description": "Return inventory items currently at or below their reorder point.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "city": {"type": "string", "description": "Optional city/warehouse filter"},
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "erp",
        "original_name": "get_distributor_profile",
        "function": {
            "name": "erp.get_distributor_profile",
            "description": (
                "Fetch distributor profile details "
                "(gateway scrubs PII fields before LLM sees them)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "distributor_id": {"type": "string", "description": "Optional distributor ID"},
                    "city": {"type": "string", "description": "Optional city filter"},
                    "name": {"type": "string", "description": "Optional distributor name search"},
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "erp",
        "original_name": "create_requisition",
        "function": {
            "name": "erp.create_requisition",
            "description": (
                "Create a purchase requisition in pending_approval status "
                "(requires human sign-off)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "material_id": {"type": "string", "description": "Material / SKU code"},
                    "quantity": {"type": "integer", "minimum": 1, "description": "Order quantity"},
                    "requested_by": {"type": "string", "description": "Requesting user ID"},
                    "city": {"type": "string", "description": "Target warehouse/city"},
                    "idempotency_key": {"type": "string", "description": "Deterministic idempotency key"},
                    "trace_id": {"type": "string", "description": "Gateway trace ID"},
                },
                "required": ["tenant_id", "material_id", "quantity"],
            },
        },
    },
    {
        "type": "function",
        "server": "knowledge",
        "original_name": "search_sops",
        "function": {
            "name": "knowledge.search_sops",
            "description": (
                "Search enterprise Standard Operating Procedures (SOPs) "
                "using BM25/keyword search."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"},
                    "tenant_id": {"type": "string", "description": "Optional tenant scope"},
                    "top_k": {"type": "integer", "default": 3, "description": "Max chunks to return"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "server": "knowledge",
        "original_name": "get_sop_document",
        "function": {
            "name": "knowledge.get_sop_document",
            "description": "Retrieve the full text of a specific SOP document by document ID or filename.",
            "parameters": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "SOP document ID or filename"},
                    "tenant_id": {"type": "string", "description": "Optional tenant scope"},
                },
                "required": ["doc_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "ops",
        "original_name": "create_escalation_ticket",
        "function": {
            "name": "ops.create_escalation_ticket",
            "description": "Create an operational escalation ticket for delayed shipments or SLA breaches.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "title": {"type": "string", "description": "Short ticket title"},
                    "severity": {
                        "type": "string",
                        "enum": ["low", "medium", "high", "critical"],
                        "default": "high",
                    },
                    "related_id": {"type": "string", "description": "Related order, SKU, or hub"},
                    "idempotency_key": {"type": "string", "description": "Deterministic idempotency key"},
                },
                "required": ["tenant_id", "title"],
            },
        },
    },
    {
        "type": "function",
        "server": "ops",
        "original_name": "list_tickets",
        "function": {
            "name": "ops.list_tickets",
            "description": "List operational escalation tickets for the tenant.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "status": {"type": "string", "description": "Optional status filter"},
                    "limit": {"type": "integer", "default": 20},
                },
                "required": ["tenant_id"],
            },
        },
    },
    {
        "type": "function",
        "server": "ops",
        "original_name": "send_ops_alert",
        "function": {
            "name": "ops.send_ops_alert",
            "description": "Send an operational alert notification (manager/admin only).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tenant_id": {"type": "string", "description": "Tenant identifier"},
                    "title": {"type": "string", "description": "Alert subject"},
                    "severity": {"type": "string", "default": "high"},
                    "related_id": {"type": "string", "default": ""},
                    "idempotency_key": {"type": "string", "description": "Deterministic idempotency key"},
                },
                "required": ["tenant_id", "title"],
            },
        },
    },
]


def _serialize_result(value: Any) -> str:
    """Normalize arbitrary Python/Pydantic tool results into a JSON string."""
    if isinstance(value, str):
        return value
    if isinstance(value, BaseModel):
        return json.dumps(value.model_dump(mode="json"), default=str)
    if isinstance(value, list):
        items = [
            v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in value
        ]
        return json.dumps(items, default=str)
    return json.dumps(value, default=str)


def _expand_env_vars(value: str, env_overrides: dict[str, str]) -> str:
    """Expand ${workspaceFolder} and ${env:VAR} placeholders in config strings."""
    resolved = value.replace("${workspaceFolder}", str(PROJECT_ROOT))
    for var_name in re.findall(r"\$\{env:([^}]+)\}", resolved):
        env_val = os.getenv(var_name)
        if env_val is None or env_val == "":
            env_val = env_overrides.get(var_name, "")
        resolved = resolved.replace(f"${{env:{var_name}}}", env_val)
    return resolved


def _expand_dict(data: dict[str, Any], env_overrides: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in data.items():
        if isinstance(v, str):
            out[k] = _expand_env_vars(v, env_overrides)
        elif isinstance(v, dict):
            out[k] = _expand_dict(v, env_overrides)
        elif isinstance(v, list):
            out[k] = [
                _expand_env_vars(item, env_overrides) if isinstance(item, str) else item
                for item in v
            ]
        else:
            out[k] = v
    return out


class InProcessMCPBackend:
    """Direct in-process dispatcher to services/mcp_erp, mcp_knowledge, and mcp_ops."""

    _SERVER_MODULES: dict[str, str] = {
        "erp": "services.mcp_erp.tools",
        "knowledge": "services.mcp_knowledge.search",
        "ops": "services.mcp_ops.tools",
    }

    _TOOL_FN_ALIASES: dict[str, str] = {
        "erp.get_open_orders": "get_open_sales_orders",
        "erp.check_inventory": "check_inventory_level",
        "erp.get_low_stock_items": "list_low_stock_items",
        "erp.create_requisition": "create_purchase_requisition",
    }

    def __init__(self) -> None:
        self._overrides: dict[str, Callable[..., Any]] = {}

    def register_handler(self, tool_name: str, handler: Callable[..., Any]) -> None:
        """Register a custom callable handler (useful for isolated unit tests)."""
        self._overrides[tool_name] = handler

    def list_tools(self) -> list[dict[str, Any]]:
        """Return deep copies of canonical in-process tool definitions."""
        return json.loads(json.dumps(INPROCESS_TOOL_DEFINITIONS))

    def _resolve_handler(self, server_name: str, original_name: str) -> Callable[..., Any]:
        full_name = f"{server_name}.{original_name}"
        if full_name in self._overrides:
            return self._overrides[full_name]
        module_path = self._SERVER_MODULES.get(server_name)
        if not module_path:
            raise ValueError(f"Unknown MCP server '{server_name}'")
        module = importlib.import_module(module_path)
        fn_name = self._TOOL_FN_ALIASES.get(full_name, original_name)
        handler = getattr(module, fn_name, None) or getattr(module, original_name, None)
        if handler is None or not callable(handler):
            raise RuntimeError(
                f"Tool handler '{fn_name}' not found in '{module_path}'."
            )
        return handler

    @staticmethod
    def _adapt_arguments(handler: Callable[..., Any], arguments: dict[str, Any]) -> dict[str, Any]:
        """Adapt common alias keys and filter kwargs to match target function signature."""
        args = dict(arguments)
        sig = inspect.signature(handler)
        params = sig.parameters
        has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

        # Alias mapping between gateway schemas and sibling MCP tool implementations
        alias_pairs = [
            ("sku", "material_id"),
            ("material_id", "sku"),
            ("warehouse", "city"),
            ("city", "warehouse"),
            ("sop_id", "doc_id"),
            ("doc_id", "sop_id"),
            ("filename", "doc_id"),
            ("doc_id", "filename"),
            ("description", "related_id"),
            ("message", "title"),
            ("title", "message"),
        ]
        for src, dst in alias_pairs:
            if src in args and dst not in args and dst in params:
                args[dst] = args[src]

        if has_var_kw:
            return args
        return {k: v for k, v in args.items() if k in params}

    @staticmethod
    def _resolve_default_distributor_id(
        tenant_id: str, city: str | None = None, name: str | None = None
    ) -> str | None:
        """Resolve a distributor_id when caller filters by tenant/city without ID."""
        try:
            from sqlalchemy import func

            from services.mcp_erp.db import SessionLocal
            from services.mcp_erp.models import Distributor

            with SessionLocal() as db:
                query = db.query(Distributor).filter(Distributor.tenant_id == tenant_id)
                if city and city.strip():
                    city_q = query.filter(func.lower(Distributor.city) == city.strip().lower())
                    match = city_q.order_by(Distributor.credit_limit.desc()).first()
                    if match:
                        return match.distributor_id
                if name and name.strip():
                    name_q = query.filter(Distributor.name.ilike(f"%{name.strip()}%"))
                    match = name_q.first()
                    if match:
                        return match.distributor_id
                first = query.order_by(Distributor.credit_limit.desc()).first()
                return first.distributor_id if first else None
        except Exception:
            return None

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Invoke the in-process tool handler and return serialized text/JSON."""
        tool_info = next(
            (t for t in INPROCESS_TOOL_DEFINITIONS if t["function"]["name"] == tool_name),
            None,
        )
        if tool_info is None:
            raise ValueError(f"Tool '{tool_name}' is not registered in InProcessMCPBackend.")
        server_name = str(tool_info["server"])
        original_name = str(tool_info["original_name"])
        handler = self._resolve_handler(server_name, original_name)

        enriched_args = dict(arguments)
        if tool_name == "ops.send_ops_alert":
            enriched_args.setdefault("channel", "ops-general")
        if (
            tool_name == "erp.get_distributor_profile"
            and not enriched_args.get("distributor_id")
            and enriched_args.get("tenant_id")
            and "erp.get_distributor_profile" not in self._overrides
        ):
            resolved_id = self._resolve_default_distributor_id(
                tenant_id=str(enriched_args["tenant_id"]),
                city=enriched_args.get("city"),
                name=enriched_args.get("name"),
            )
            if resolved_id:
                enriched_args["distributor_id"] = resolved_id

        adapted_args = self._adapt_arguments(handler, enriched_args)

        result = handler(**adapted_args)
        if inspect.isawaitable(result):
            result = await result
        return _serialize_result(result)


class MCPClientManager:
    """Manages MCP tool discovery and invocation across remote or in-process backends."""

    def __init__(
        self,
        backend_mode: str | None = None,
        config_path: str | Path | None = None,
    ) -> None:
        settings = get_settings()
        self.backend_mode = (
            backend_mode or os.getenv("MCP_BACKEND_MODE") or "inprocess"
        ).strip().lower()
        self.config_path = Path(config_path or settings.mcp_servers_config_path)
        self.sessions: dict[str, ClientSession] = {}
        self.all_tools: list[dict[str, Any]] = []
        self.exit_stack = AsyncExitStack()
        self.inprocess = InProcessMCPBackend()
        self._initialized = False

    async def initialize(self) -> None:
        """Initialize configured backend (inprocess or remote MCP servers)."""
        if self._initialized:
            return
        if self.backend_mode == "inprocess":
            self.all_tools = self.inprocess.list_tools()
            self._initialized = True
            logger.info(
                f"MCPClientManager initialized in 'inprocess' mode with {len(self.all_tools)} tools."
            )
            return

        await self.load_servers(str(self.config_path))
        self._initialized = True

    async def load_servers(self, config_path: str) -> None:
        """Load and connect to remote MCP servers from configs/mcp_servers.json."""
        path = Path(config_path)
        if not path.exists():
            raise FileNotFoundError(f"MCP servers config not found: {path}")
        config = json.loads(path.read_text(encoding="utf-8"))
        servers = config.get("mcpServers")
        if not isinstance(servers, dict):
            raise ValueError(f"Config '{path}' missing 'mcpServers' object.")

        settings = get_settings()
        env_overrides = {
            "ERP_MCP_URL": settings.erp_mcp_url,
            "KNOWLEDGE_MCP_URL": settings.knowledge_mcp_url,
            "MCP_API_KEY": settings.mcp_api_key,
            "DATABASE_URL": settings.database_url,
            "PYTHON_EXECUTABLE": sys.executable,
        }

        for name, raw_cfg in servers.items():
            resolved = _expand_dict(dict(raw_cfg), env_overrides)
            await self._connect_server(str(name), resolved)

    async def _connect_server(self, name: str, config: dict[str, Any]) -> None:
        """Connect to one remote MCP server over stdio, sse, or streamable_http."""
        server_stack = AsyncExitStack()
        try:
            transport_type = str(config.get("transport", "")).lower()
            if "command" in config or transport_type == "stdio":
                env = dict(os.environ)
                env.update({str(k): str(v) for k, v in (config.get("env") or {}).items()})
                params = StdioServerParameters(
                    command=str(config["command"]),
                    args=[str(a) for a in config.get("args", [])],
                    env=env,
                    cwd=str(config.get("cwd") or PROJECT_ROOT),
                )
                read, write = await server_stack.enter_async_context(stdio_client(params))
            elif "url" in config:
                url = str(config["url"])
                headers = {str(k): str(v) for k, v in (config.get("headers") or {}).items()}
                if transport_type == "sse" or url.rstrip("/").endswith("/sse"):
                    read, write = await server_stack.enter_async_context(
                        sse_client(url=url, headers=headers)
                    )
                else:
                    read, write, _ = await server_stack.enter_async_context(
                        streamablehttp_client(url=url, headers=headers)
                    )
            else:
                raise ValueError(f"Invalid MCP server config for '{name}'.")

            session = await server_stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self.sessions[name] = session
            meta = await session.list_tools()

            for tool in meta.tools:
                self.all_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": f"{name}.{tool.name}",
                            "description": tool.description or "",
                            "parameters": tool.inputSchema or {"type": "object", "properties": {}},
                        },
                        "server": name,
                        "original_name": tool.name,
                    }
                )

            self.exit_stack.push_async_callback(server_stack.pop_all().aclose)
            logger.info(f"Connected remote MCP server '{name}' with {len(meta.tools)} tools.")
        except BaseException as exc:
            logger.error(f"Failed to connect to remote MCP server '{name}': {exc!r}")
            try:
                await server_stack.aclose()
            except BaseException:
                pass

    def list_tools(self) -> list[dict[str, Any]]:
        """Return all discovered tools."""
        return list(self.all_tools)

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """Execute a tool on either the in-process backend or a remote MCP session."""
        if not self._initialized:
            await self.initialize()

        if self.backend_mode == "inprocess":
            return await self.inprocess.call_tool(tool_name, arguments)

        tool_info = next(
            (t for t in self.all_tools if t["function"]["name"] == tool_name),
            None,
        )
        if not tool_info:
            raise ValueError(f"Tool '{tool_name}' not found on remote MCP sessions.")
        server_name = str(tool_info["server"])
        original_name = str(tool_info["original_name"])
        session = self.sessions[server_name]
        result = await session.call_tool(original_name, arguments)
        if not result or not getattr(result, "content", None):
            return json.dumps({"status": "empty", "content": []})

        texts: list[str] = []
        for item in result.content:
            if hasattr(item, "text"):
                texts.append(str(item.text))
            elif hasattr(item, "data"):
                texts.append(_serialize_result(item.data))
            else:
                texts.append(str(item))
        return "\n".join(texts)

    async def cleanup(self) -> None:
        """Close all active remote MCP sessions."""
        try:
            await self.exit_stack.aclose()
        except Exception as exc:
            logger.debug(f"Error closing MCP exit stack: {exc}")
        self.sessions.clear()
        self._initialized = False
