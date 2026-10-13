"""
Performance optimizations for high-traffic scenarios.
Includes rate limiting and caching.
"""

import time
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
    """

    def __init__(self, app):
        super().__init__(app)
        self.requests: dict = {}  # {ip: [(timestamp, path)]}

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

        # Find the most specific matching limit (longest prefix wins, so the
        # result does not depend on the order of the limits).
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

        # Initialize or clean up
        if client_ip not in self.requests:
            if len(self.requests) >= config.rate_limit.max_clients:
                oldest_client = min(
                    self.requests,
                    key=lambda client: (
                        self.requests[client][-1][0] if self.requests[client] else 0
                    ),
                )
                del self.requests[oldest_client]
            self.requests[client_ip] = []

        # Remove old requests
        self.requests[client_ip] = [
            (ts, p) for ts, p in self.requests[client_ip] if ts > window_start
        ]

        # Check limit for this path
        path_requests = len([p for _, p in self.requests[client_ip] if p == path])

        if path_requests >= limit:
            logger.warning(
                "Rate limit exceeded for {client} on {path}",
                client=client_ip,
                path=path,
            )
            return Response(
                content=config.messages.rate_limit_exceeded,
                status_code=HTTP_429_TOO_MANY_REQUESTS,
            )

        # Record request
        self.requests[client_ip].append((current_time, path))

        return await call_next(request)


class CacheStore:
    """Simple in-memory cache with TTL."""

    def __init__(self):
        self.cache: dict = {}  # {key: (value, expires_at)}

    def get(self, key: str) -> Any | None:
        """Get value from cache if not expired."""
        if key in self.cache:
            value, expires_at = self.cache[key]
            if time.time() < expires_at:
                return value
            del self.cache[key]
        return None

    def set(self, key: str, value: Any, ttl_seconds: int | None = None) -> None:
        """Store value with TTL."""
        if ttl_seconds is None:
            ttl_seconds = config.cache.ttl_seconds
        self.cache[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, pattern: str = "") -> None:
        """Invalidate cache entries matching pattern."""
        if not pattern:
            self.cache.clear()
        else:
            self.cache = {k: v for k, v in self.cache.items() if pattern not in k}


# Global cache instance
cache_store = CacheStore()
