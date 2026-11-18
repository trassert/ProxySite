"""
Performance optimizations for high-traffic scenarios.
Includes rate limiting and caching.
"""

import json
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from config import config, logger


class RateLimiter(BaseHTTPMiddleware):
    """
    Token bucket rate limiter.
    Prevents API abuse and protects against DDoS.
    Uses bounded per-IP deques to avoid unbounded memory growth.
    """

    def __init__(self, app):
        super().__init__(app)
        self.requests: dict[str, deque] = {}  # {ip: deque of timestamps}
        self.max_clients = config.rate_limit.max_clients

    @staticmethod
    def _limits() -> dict[str, int]:
        """Per-IP rate limits (requests per window), read from config."""
        return {
            "/api/vote/": config.rate_limit.limit_vote_get,
            "/api/vote": config.rate_limit.limit_vote_post,
            "/api/proxies": config.rate_limit.limit_proxies,
            "/api/add-proxy": config.rate_limit.limit_add_proxy,
            "/": config.rate_limit.limit_homepage,
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Check rate limit before processing."""
        if not config.rate_limit.enabled:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path

        # Find the most specific matching limit (longest prefix wins).
        limit = None
        for prefix, rate in self._limits().items():
            if path.startswith(prefix) and (
                limit is None or len(prefix) > len(limit[0])
            ):
                limit = (prefix, rate)
        limit = limit[1] if limit else None

        if limit is None:
            return await call_next(request)

        current_time = time.time()
        window_start = current_time - config.rate_limit.window_seconds

        # Initialize or reuse bounded deque per IP
        if client_ip not in self.requests:
            # Evict oldest IP when at capacity (O(1) vs O(N) min scan)
            if len(self.requests) >= self.max_clients:
                self.requests.pop(next(iter(self.requests)))
            self.requests[client_ip] = deque(maxlen=256)

        dq = self.requests[client_ip]

        # Remove old requests outside the window
        while dq and dq[0] <= window_start:
            dq.popleft()

        # Check limit
        if len(dq) >= limit:
            # Throttle logging: only log if we haven't logged for this IP recently
            # or just log once per limit event.
            if len(dq) == limit: # Log only on the first violation
                logger.warning(
                    "Rate limit exceeded for {client} on {path}",
                    client=client_ip,
                    path=path,
                )
            return Response(
                content=json.dumps({"detail": config.messages.rate_limit_exceeded}),
                status_code=HTTP_429_TOO_MANY_REQUESTS,
                media_type="application/json",
            )

        dq.append(current_time)
        return await call_next(request)


class CacheStore:
    """LRU in-memory cache with size limit and TTL."""

    def __init__(self, maxsize: int = 512):
        self.cache: OrderedDict = OrderedDict()
        self.maxsize = maxsize

    def get(self, key: str) -> Any | None:
        """Get value from cache if not expired."""
        if key in self.cache:
            value, expires_at = self.cache[key]
            if time.time() < expires_at:
                # Move to end (most recently used)
                self.cache.move_to_end(key)
                return value
            del self.cache[key]
        return None

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store value with TTL, evicting LRU entry if full."""
        if ttl_seconds is None:
            ttl_seconds = config.cache.ttl_seconds
        if key in self.cache:
            self.cache.move_to_end(key)
        elif len(self.cache) >= self.maxsize:
            self.cache.popitem(last=False)
        self.cache[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, pattern: str = "") -> None:
        """Invalidate cache entries matching pattern."""
        if not pattern:
            self.cache.clear()
        else:
            to_delete = [k for k in self.cache if pattern in k]
            for k in to_delete:
                del self.cache[k]


# Global cache instance with reasonable LRU capacity
cache_store = CacheStore(maxsize=512)
