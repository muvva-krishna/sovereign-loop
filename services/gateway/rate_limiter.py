"""Sliding-window rate limiter with Redis backend and thread-safe in-memory fallback."""

from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from loguru import logger

try:
    import redis.asyncio as redis_async
except ImportError:  # pragma: no cover - redis is in dependencies
    redis_async = None  # type: ignore[assignment]


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a rate-limit check."""

    allowed: bool
    remaining: int
    limit: int
    retry_after_seconds: float = 0.0


class RateLimiter:
    """Sliding-window rate limiter supporting Redis with automatic in-memory fallback."""

    def __init__(self, redis_url: str = "", window_seconds: int = 60) -> None:
        self.redis_url = redis_url.strip()
        self.window_seconds = max(1, int(window_seconds))
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._redis: Any | None = None
        self._redis_disabled = False

    def _get_redis(self) -> Any | None:
        if not self.redis_url or self._redis_disabled or redis_async is None:
            return None
        if self._redis is None:
            self._redis = redis_async.from_url(
                self.redis_url,
                encoding="utf-8",
                decode_responses=True,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        return self._redis

    def _check_memory(self, key: str, limit: int) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            bucket = self._buckets[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(0.1, self.window_seconds - (now - bucket[0]))
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    limit=limit,
                    retry_after_seconds=round(retry_after, 2),
                )
            bucket.append(now)
            remaining = max(0, limit - len(bucket))
            return RateLimitResult(allowed=True, remaining=remaining, limit=limit)

    async def _check_redis(self, client: Any, key: str, limit: int) -> RateLimitResult:
        now = time.time()
        cutoff = now - self.window_seconds
        redis_key = f"ratelimit:{key}"
        member = f"{now}:{uuid.uuid4().hex}"

        pipe = client.pipeline()
        pipe.zremrangebyscore(redis_key, "-inf", cutoff)
        pipe.zcard(redis_key)
        _, current_count = await pipe.execute()

        if int(current_count) >= limit:
            oldest = await client.zrange(redis_key, 0, 0, withscores=True)
            retry_after = float(self.window_seconds)
            if oldest:
                retry_after = max(0.1, self.window_seconds - (now - float(oldest[0][1])))
            return RateLimitResult(
                allowed=False,
                remaining=0,
                limit=limit,
                retry_after_seconds=round(retry_after, 2),
            )

        add_pipe = client.pipeline()
        add_pipe.zadd(redis_key, {member: now})
        add_pipe.expire(redis_key, self.window_seconds + 5)
        await add_pipe.execute()

        remaining = max(0, limit - int(current_count) - 1)
        return RateLimitResult(allowed=True, remaining=remaining, limit=limit)

    async def check_and_consume(self, key: str, limit: int) -> RateLimitResult:
        """Check whether key is within its RPM limit and record one request if allowed."""
        effective_limit = max(1, int(limit))
        client = self._get_redis()
        if client is not None:
            try:
                return await self._check_redis(client, key, effective_limit)
            except Exception as exc:
                logger.warning(f"Redis rate limiter unavailable ({exc}); falling back to in-memory.")
                self._redis_disabled = True
        return self._check_memory(key, effective_limit)

    async def close(self) -> None:
        """Close underlying Redis connection if open."""
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception:
                pass
            self._redis = None
