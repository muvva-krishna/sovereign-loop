"""Structured JSON trace persistence and optional Langfuse observability bridge."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger

from contracts.config import get_settings


class TraceRecorder:
    """Persists and queries execution traces in data/traces/<trace_id>.json."""

    def __init__(self, traces_dir: str | Path | None = None) -> None:
        settings = get_settings()
        self.traces_dir = Path(traces_dir or settings.traces_dir)
        self.traces_dir.mkdir(parents=True, exist_ok=True)

    def _trace_path(self, trace_id: str) -> Path:
        safe_id = "".join(c for c in trace_id if c.isalnum() or c in {"-", "_"}) or "unknown"
        return self.traces_dir / f"{safe_id}.json"

    def record_trace(self, trace_data: dict[str, Any]) -> Path:
        """Write structured JSON trace to disk atomically."""
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        trace_id = str(trace_data.get("trace_id", "unknown"))
        payload = {
            **trace_data,
            "recorded_at": trace_data.get("recorded_at")
            or datetime.now(timezone.utc).isoformat(),
        }
        path = self._trace_path(trace_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        tmp_path.replace(path)
        return path

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        """Load a single trace by trace_id if it exists."""
        path = self._trace_path(trace_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.warning(f"Failed to parse trace file {path}: {exc}")
            return None

    def list_traces(self, limit: int = 20) -> list[dict[str, Any]]:
        """List most recent traces ordered by file modification time descending."""
        if not self.traces_dir.exists():
            return []
        files = sorted(
            (p for p in self.traces_dir.glob("*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        traces: list[dict[str, Any]] = []
        for path in files[: max(1, limit)]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    traces.append(data)
            except Exception as exc:
                logger.warning(f"Skipping unreadable trace {path}: {exc}")
        return traces


class LangfuseTracer:
    """Optional Langfuse v2/v5-compatible tracer that safely no-ops when disabled."""

    def __init__(self) -> None:
        settings = get_settings()
        self.enabled = bool(
            settings.langfuse_enabled
            and settings.langfuse_public_key.strip()
            and settings.langfuse_secret_key.strip()
        )
        self._client: Any | None = None
        if self.enabled:
            try:
                from langfuse import Langfuse

                self._client = Langfuse(
                    public_key=settings.langfuse_public_key,
                    secret_key=settings.langfuse_secret_key,
                    host=settings.langfuse_host,
                )
            except Exception as exc:
                logger.warning(f"Langfuse initialization failed ({exc}); disabling Langfuse.")
                self.enabled = False
                self._client = None

    def record_gateway_trace(self, trace_data: dict[str, Any]) -> None:
        """Emit a trace and tool spans to Langfuse if enabled; never raises."""
        if not self.enabled or self._client is None:
            return
        try:
            trace_id = str(trace_data.get("trace_id", ""))
            metadata = {
                "tenant_id": trace_data.get("tenant_id"),
                "user_id": trace_data.get("user_id"),
                "role": trace_data.get("role"),
                "model_used": trace_data.get("model_used"),
                "fallback_triggered": trace_data.get("fallback_triggered"),
                "pii_redacted_count": trace_data.get("pii_redacted_count", 0),
                "total_latency_ms": trace_data.get("total_latency_ms", 0.0),
            }
            # Langfuse v5+ OpenTelemetry observation API
            if hasattr(self._client, "start_observation"):
                root = self._client.start_observation(
                    name="gateway.chat",
                    as_type="span",
                    input={"prompt": trace_data.get("prompt", "")},
                    output={"answer": trace_data.get("answer", "")},
                    metadata={**metadata, "gateway_trace_id": trace_id},
                )
                for tool_trace in trace_data.get("tool_traces", []):
                    child = self._client.start_observation(
                        name=f"tool:{tool_trace.get('tool_name', 'unknown')}",
                        as_type="tool",
                        input=tool_trace.get("arguments", {}),
                        output={"preview": tool_trace.get("result_preview", "")},
                        metadata={
                            "status": tool_trace.get("status"),
                            "policy": tool_trace.get("policy_decision"),
                            "duration_ms": tool_trace.get("duration_ms"),
                        },
                    )
                    if hasattr(child, "end"):
                        child.end()
                if hasattr(root, "end"):
                    root.end()
            # Langfuse v2 legacy API fallback
            elif hasattr(self._client, "trace"):
                lf_trace = self._client.trace(
                    id=trace_id or None,
                    name="gateway.chat",
                    user_id=str(trace_data.get("user_id", "")),
                    input=trace_data.get("prompt", ""),
                    output=trace_data.get("answer", ""),
                    metadata=metadata,
                )
                for tool_trace in trace_data.get("tool_traces", []):
                    lf_trace.span(
                        name=f"tool:{tool_trace.get('tool_name', 'unknown')}",
                        input=tool_trace.get("arguments", {}),
                        output=tool_trace.get("result_preview", ""),
                        metadata={
                            "status": tool_trace.get("status"),
                            "policy": tool_trace.get("policy_decision"),
                        },
                    )
            if hasattr(self._client, "flush"):
                self._client.flush()
        except Exception as exc:
            logger.debug(f"Langfuse trace export skipped due to error: {exc}")

    def shutdown(self) -> None:
        """Flush and close Langfuse client if initialized."""
        if self._client is not None:
            try:
                if hasattr(self._client, "flush"):
                    self._client.flush()
                if hasattr(self._client, "shutdown"):
                    self._client.shutdown()
            except Exception:
                pass
