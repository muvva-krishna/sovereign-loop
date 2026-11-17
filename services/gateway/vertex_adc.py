"""Async Google Cloud Vertex AI Gemini client using Application Default Credentials (ADC).

Adapted from job-intelligence's Vertex ADC architecture:
- Forces IPv4 socket resolution to prevent 45s IPv6 connect hangs on Linux.
- Mints bearer access tokens directly from ADC OAuth2 refresh credentials via
  https://oauth2.googleapis.com/token (with gcloud CLI fallback), zero API keys required.
- Preserves Gemini `thoughtSignature` across multi-round function calling turns.
- Computes USD cost directly from Vertex AI `usageMetadata`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import re
import socket
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from contracts.config import get_settings

_ORIG_GETADDRINFO = socket.getaddrinfo


def _ipv4_getaddrinfo(
    host: Any,
    port: Any,
    family: int = 0,
    type: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    try:
        return _ORIG_GETADDRINFO(host, port, socket.AF_INET, type, proto, flags)
    except OSError:
        return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)


socket.getaddrinfo = _ipv4_getaddrinfo

# Official Vertex AI Standard-tier pricing (USD per 1M tokens) matching job-intelligence.
VERTEX_PRICING_PER_1M: dict[str, dict[str, Any]] = {
    "gemini-3.8-flash": {
        "input": 0.75,
        "output": 3.75,
        "cached_input": 0.075,
        "intro_through": "2026-12-31",
        "post_intro": {"input": 1.50, "output": 7.50, "cached_input": 0.15},
    },
    "gemini-3.5-flash-lite": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cached_input": 0.025},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50, "cached_input": 0.03},
    "gemini-2.5-flash-lite": {"input": 0.10, "output": 0.40, "cached_input": 0.01},
    "gemini-3.8-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.00, "cached_input": 0.125},
}
NON_GLOBAL_FACTOR = 1.1

_DEFAULT_ADC_PATH = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
_DEFAULT_GCLOUD_BIN = Path.home() / "google-cloud-sdk" / "bin" / "gcloud"


def is_vertex_gemini_model(model: str | None) -> bool:
    """Return True if model identifier refers to a Vertex AI / Gemini model."""
    if not model:
        return False
    cleaned = model.strip().lower()
    return (
        cleaned.startswith("gemini-")
        or cleaned.startswith("vertex/")
        or cleaned.startswith("vertex_ai/")
    )


def strip_vertex_prefix(model: str) -> str:
    """Normalize optional `vertex/` or `vertex_ai/` prefix to bare Gemini model ID."""
    cleaned = model.strip()
    for prefix in ("vertex_ai/", "vertex/"):
        if cleaned.lower().startswith(prefix):
            return cleaned[len(prefix) :]
    return cleaned


def _to_vertex_tool_name(name: str) -> str:
    return name.replace(".", "__")


def _normalize_model_family(model: str) -> str:
    m = strip_vertex_prefix(model).lower()
    m = re.sub(r"-(?:\d{3}|preview|experimental|latest)$", "", m)
    return m or "gemini-3.8-flash"


def compute_vertex_cost_usd(
    model: str,
    usage_metadata: dict[str, Any],
    *,
    location: str = "global",
) -> tuple[int, float]:
    """Compute (total_tokens, usd_cost) from Vertex AI response usageMetadata."""
    prompt_tokens = int(usage_metadata.get("promptTokenCount") or 0)
    cached_tokens = int(usage_metadata.get("cachedContentTokenCount") or 0)
    candidates_tokens = int(usage_metadata.get("candidatesTokenCount") or 0)
    thoughts_tokens = int(usage_metadata.get("thoughtsTokenCount") or 0)
    total_tokens = int(
        usage_metadata.get("totalTokenCount")
        or (prompt_tokens + candidates_tokens + thoughts_tokens)
    )

    family = _normalize_model_family(model)
    entry = VERTEX_PRICING_PER_1M.get(family)
    if entry is None:
        if family.endswith("lite"):
            entry = VERTEX_PRICING_PER_1M["gemini-3.5-flash-lite"]
        elif family.endswith("pro"):
            entry = VERTEX_PRICING_PER_1M["gemini-3.8-pro"]
        else:
            entry = VERTEX_PRICING_PER_1M["gemini-3.8-flash"]

    today = dt.date.today().isoformat()
    if isinstance(entry.get("post_intro"), dict) and today > str(entry.get("intro_through", "")):
        rates = dict(entry["post_intro"])
    else:
        rates = {
            "input": entry["input"],
            "output": entry["output"],
            "cached_input": entry["cached_input"],
        }

    factor = NON_GLOBAL_FACTOR if (location or "global").lower() not in {"global", ""} else 1.0
    non_cached_input = max(0, prompt_tokens - cached_tokens)
    output_billed = candidates_tokens + thoughts_tokens
    cost = (
        (non_cached_input / 1_000_000.0) * rates["input"] * factor
        + (cached_tokens / 1_000_000.0) * rates["cached_input"] * factor
        + (output_billed / 1_000_000.0) * rates["output"] * factor
    )
    return total_tokens, round(cost, 6)


def _sanitize_vertex_schema(schema: Any) -> dict[str, Any]:
    """Normalize JSON Schema dicts for Vertex Gemini functionDeclarations compatibility."""
    if not isinstance(schema, dict):
        return {"type": "string"}

    # Handle Pydantic/FastMCP `anyOf: [{"type": "string"}, {"type": "null"}]`
    if "anyOf" in schema and isinstance(schema["anyOf"], list):
        non_null = [
            s for s in schema["anyOf"] if isinstance(s, dict) and s.get("type") != "null"
        ]
        chosen = _sanitize_vertex_schema(non_null[0]) if non_null else {"type": "string"}
        if "description" in schema and "description" not in chosen:
            chosen["description"] = str(schema["description"])
        return chosen

    cleaned: dict[str, Any] = {}
    for key in ("type", "description", "enum", "required"):
        if key in schema:
            cleaned[key] = schema[key]

    if "properties" in schema and isinstance(schema["properties"], dict):
        cleaned["properties"] = {
            str(k): _sanitize_vertex_schema(v) for k, v in schema["properties"].items()
        }
        cleaned.setdefault("type", "object")
    if "items" in schema and isinstance(schema["items"], dict):
        cleaned["items"] = _sanitize_vertex_schema(schema["items"])

    cleaned.setdefault("type", "object" if "properties" in cleaned else "string")
    return cleaned


def _vertex_endpoint(project: str, location: str, model: str) -> str:
    bare_model = strip_vertex_prefix(model)
    if location == "global":
        host = "aiplatform.googleapis.com"
    elif location in {"us", "eu"}:
        host = f"aiplatform.{location}.rep.googleapis.com"
    else:
        host = f"{location}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{project}"
        f"/locations/{location}/publishers/google/models/{bare_model}:generateContent"
    )


class VertexADCClient:
    """Async Vertex AI Gemini client authenticating via Google ADC (no API keys)."""

    def __init__(self) -> None:
        self._token_cache: dict[str, Any] = {"token": "", "expires_at": 0.0}
        self._token_lock = asyncio.Lock()
        self._thought_signatures: dict[str, str] = {}

    def _resolve_adc_path(self) -> Path:
        settings = get_settings()
        env_path = (
            settings.google_application_credentials
            or os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
        ).strip()
        return Path(env_path) if env_path else _DEFAULT_ADC_PATH

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Mint or return a cached OAuth2 ADC access token (~0.3s via refresh token)."""
        async with self._token_lock:
            now = time.time()
            if (
                not force_refresh
                and self._token_cache["token"]
                and now < float(self._token_cache["expires_at"]) - 60.0
            ):
                return str(self._token_cache["token"])

            adc_path = self._resolve_adc_path()
            if adc_path.exists():
                try:
                    creds = json.loads(adc_path.read_text(encoding="utf-8"))
                    if all(k in creds for k in ("client_id", "client_secret", "refresh_token")):
                        async with httpx.AsyncClient(timeout=15.0) as client:
                            resp = await client.post(
                                "https://oauth2.googleapis.com/token",
                                data={
                                    "client_id": creds["client_id"],
                                    "client_secret": creds["client_secret"],
                                    "refresh_token": creds["refresh_token"],
                                    "grant_type": "refresh_token",
                                },
                            )
                            resp.raise_for_status()
                            tok_data = resp.json()
                        tok = str(tok_data.get("access_token") or "").strip()
                        if tok:
                            expires_in = int(tok_data.get("expires_in", 3500))
                            self._token_cache = {"token": tok, "expires_at": now + expires_in}
                            return tok
                except Exception as exc:
                    logger.warning(f"Direct ADC token refresh failed ({exc}); trying gcloud CLI.")

            gcloud_cmd = (
                str(_DEFAULT_GCLOUD_BIN)
                if _DEFAULT_GCLOUD_BIN.exists()
                else os.getenv("GCLOUD_BIN", "gcloud")
            )
            try:
                tok = await asyncio.to_thread(
                    lambda: subprocess.check_output(
                        [gcloud_cmd, "auth", "application-default", "print-access-token"],
                        text=True,
                        timeout=15,
                    ).strip()
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Unable to mint Google ADC token from {adc_path} or gcloud CLI: {exc}"
                ) from exc

            if not tok:
                raise RuntimeError("Empty access token returned by Google ADC.")
            self._token_cache = {"token": tok, "expires_at": now + 3000.0}
            return tok

    def _build_request_body(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[dict[str, Any], dict[str, str]]:
        """Translate OpenAI chat messages/tools to Vertex AI `:generateContent` JSON."""
        system_chunks: list[str] = []
        contents: list[dict[str, Any]] = []

        idx = 0
        while idx < len(messages):
            msg = messages[idx]
            role = str(msg.get("role", "user"))
            if role == "system":
                text = str(msg.get("content") or "").strip()
                if text:
                    system_chunks.append(text)
                idx += 1
                continue

            if role == "user":
                contents.append({"role": "user", "parts": [{"text": str(msg.get("content") or "")}]})
                idx += 1
                continue

            if role == "assistant":
                parts: list[dict[str, Any]] = []
                text = str(msg.get("content") or "").strip()
                if text:
                    parts.append({"text": text})
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    call_id = str(tc.get("id") or "")
                    canon_name = str(fn.get("name") or "")
                    raw_args = fn.get("arguments", "{}")
                    if isinstance(raw_args, str):
                        try:
                            args_obj = json.loads(raw_args) if raw_args.strip() else {}
                        except json.JSONDecodeError:
                            args_obj = {}
                    elif isinstance(raw_args, dict):
                        args_obj = raw_args
                    else:
                        args_obj = {}
                    fc_part: dict[str, Any] = {
                        "functionCall": {
                            "name": _to_vertex_tool_name(canon_name),
                            "args": args_obj,
                        }
                    }
                    sig = self._thought_signatures.get(call_id)
                    if sig:
                        fc_part["thoughtSignature"] = sig
                    parts.append(fc_part)
                if parts:
                    contents.append({"role": "model", "parts": parts})
                idx += 1
                continue

            if role == "tool":
                # Batch contiguous tool responses into one Vertex user turn
                resp_parts: list[dict[str, Any]] = []
                while idx < len(messages) and messages[idx].get("role") == "tool":
                    tmsg = messages[idx]
                    tname = _to_vertex_tool_name(str(tmsg.get("name") or "tool"))
                    raw_content = str(tmsg.get("content") or "")
                    try:
                        parsed = json.loads(raw_content)
                        resp_obj = parsed if isinstance(parsed, dict) else {"result": parsed}
                    except json.JSONDecodeError:
                        resp_obj = {"result": raw_content}
                    resp_parts.append(
                        {"functionResponse": {"name": tname, "response": resp_obj}}
                    )
                    idx += 1
                contents.append({"role": "user", "parts": resp_parts})
                continue

            idx += 1

        body: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4096},
        }
        if system_chunks:
            body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_chunks)}]}

        vertex_to_canon: dict[str, str] = {}
        if tools:
            declarations: list[dict[str, Any]] = []
            for tool in tools:
                raw_fn = tool.get("function")
                tool_fn: dict[str, Any] = raw_fn if isinstance(raw_fn, dict) else tool
                canon_name = str(tool_fn.get("name") or "")
                if not canon_name:
                    continue
                vname = _to_vertex_tool_name(canon_name)
                vertex_to_canon[vname] = canon_name
                declarations.append(
                    {
                        "name": vname,
                        "description": str(tool_fn.get("description") or canon_name),
                        "parameters": _sanitize_vertex_schema(tool_fn.get("parameters") or {}),
                    }
                )
            if declarations:
                body["tools"] = [{"functionDeclarations": declarations}]
                body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}

        return body, vertex_to_canon

    async def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout: float = 30.0,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """Execute a Vertex AI Gemini `:generateContent` call authenticated via ADC."""
        settings = get_settings()
        project = settings.vertex_project_id
        location = settings.vertex_location
        bare_model = strip_vertex_prefix(model)
        url = _vertex_endpoint(project, location, bare_model)
        body, vertex_to_canon = self._build_request_body(messages, tools)

        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            token = await self.get_access_token(force_refresh=(attempt > 0))
            try:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    resp = await client.post(
                        url,
                        json=body,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                if resp.status_code == 401 and attempt < max_retries:
                    await self.get_access_token(force_refresh=True)
                    continue
                if resp.status_code in {408, 425, 429, 500, 502, 503, 504} and attempt < max_retries:
                    delay = min(4.0, 0.5 * (2**attempt))
                    logger.warning(
                        f"Vertex AI '{bare_model}' HTTP {resp.status_code}; retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as exc:
                last_exc = exc
                if attempt < max_retries:
                    delay = min(4.0, 0.5 * (2**attempt))
                    logger.warning(
                        f"Vertex AI '{bare_model}' call failed ({exc}); retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                assert last_exc is not None
                raise last_exc

        cand = (data.get("candidates") or [{}])[0]
        parts = (cand.get("content") or {}).get("parts") or []

        text_parts: list[str] = []
        parsed_calls: list[dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if "functionCall" in part and isinstance(part["functionCall"], dict):
                fc = part["functionCall"]
                vname = str(fc.get("name") or "")
                canon_name = vertex_to_canon.get(vname, vname.replace("__", "."))
                args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                call_id = str(fc.get("id") or f"call_{uuid.uuid4().hex[:10]}")
                sig = part.get("thoughtSignature")
                if isinstance(sig, str) and sig:
                    if len(self._thought_signatures) > 512:
                        self._thought_signatures.clear()
                    self._thought_signatures[call_id] = sig
                parsed_calls.append({"id": call_id, "name": canon_name, "arguments": args})
            elif part.get("text") and not part.get("thought"):
                text_parts.append(str(part["text"]))

        usage_meta = data.get("usageMetadata") or {}
        tokens_used, cost_usd = compute_vertex_cost_usd(
            bare_model, usage_meta, location=location
        )
        return {
            "content": "".join(text_parts).strip(),
            "tool_calls": parsed_calls,
            "model_used": bare_model,
            "tokens_used": tokens_used,
            "cost_usd": cost_usd,
        }
