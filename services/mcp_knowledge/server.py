from __future__ import annotations

import os
import secrets
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from contracts.config import get_settings

from .search import (
    SopDocument,
    SopSearchHit,
)
from .search import (
    get_sop_document as _get_sop_document,
)
from .search import (
    search_sops as _search_sops,
)


def _resolve_mcp_api_key() -> str:
    if "MCP_API_KEY" in os.environ:
        return os.environ["MCP_API_KEY"]
    return get_settings().mcp_api_key


class MCPAuthMiddleware:
    """ASGI middleware enforcing MCP_API_KEY on /mcp endpoints (fails closed if unset)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path == "/mcp" or path.startswith("/mcp/"):
                expected_key = _resolve_mcp_api_key()
                if not expected_key:
                    res = JSONResponse(
                        {"error": "Server configuration error: MCP_API_KEY not set"},
                        status_code=500,
                    )
                    await res(scope, receive, send)
                    return

                headers = Headers(scope=scope)
                auth_header = headers.get("authorization", "")
                token: str | None = None
                if auth_header.lower().startswith("bearer "):
                    token = auth_header[7:].strip()
                elif "x-mcp-api-key" in headers:
                    token = headers.get("x-mcp-api-key")

                if not token or not secrets.compare_digest(token, expected_key):
                    res = JSONResponse({"error": "Unauthorized"}, status_code=401)
                    await res(scope, receive, send)
                    return

        await self.app(scope, receive, send)


class AuthFastMCP(FastMCP):
    """FastMCP subclass wrapping Streamable HTTP transport with MCPAuthMiddleware."""

    def streamable_http_app(self) -> Any:
        app = super().streamable_http_app()
        return MCPAuthMiddleware(app)


_MCP_HOST = os.environ.get("MCP_HOST", "127.0.0.1")
_MCP_PORT = int(os.environ.get("KNOWLEDGE_MCP_PORT", os.environ.get("MCP_PORT", "8102")))

mcp = AuthFastMCP("sovereign-knowledge", host=_MCP_HOST, port=_MCP_PORT)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def search_sops(tenant_id: str, query: str, top_k: int = 3) -> list[SopSearchHit]:
    """Search SOP markdown chunks visible to tenant_id using hybrid BM25/TF-IDF ranking."""
    return _search_sops(tenant_id=tenant_id, query=query, top_k=top_k)


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
)
def get_sop_document(tenant_id: str, doc_id: str) -> SopDocument:
    """Retrieve a full SOP document by doc_id, enforcing tenant_scope visibility."""
    return _get_sop_document(tenant_id=tenant_id, doc_id=doc_id)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(_request: Request) -> JSONResponse:
    """Service liveness endpoint."""
    return JSONResponse({"status": "ok", "service": "mcp_knowledge"})


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="streamable-http")
