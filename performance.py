"""
Performance optimizations for high-traffic scenarios.
Includes rate limiting, caching, and batch operations.
"""

import asyncio
import hashlib
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from config import logger


class RateLimiter(BaseHTTPMiddleware):
    """
    Token bucket rate limiter.
    Prevents API abuse and protects against DDoS.
    """

    # Per-IP rate limits (requests per minute)
    LIMITS = {
        "/api/vote/": 60,  # 1 per second
        "/api/proxies": 30,  # 0.5 per second
        "/api/add-proxy": 10,  # Very restrictive
        "/": 120,  # Homepage
    }

    def __init__(self, app):
        super().__init__(app)
        self.requests: dict = {}  # {ip: [(timestamp, path)]}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Check rate limit before processing."""
        client_ip = request.client.host if request.client else "unknown"
        path = request.url.path

        # Find matching limit
        limit = self.LIMITS.get(path)
        for prefix, rate in self.LIMITS.items():
            if path.startswith(prefix):
                limit = rate
                break

        if limit is None:
            return await call_next(request)

        current_time = time.time()
        one_minute_ago = current_time - 60

        # Initialize or clean up
        if client_ip not in self.requests:
            self.requests[client_ip] = []

        # Remove old requests
        self.requests[client_ip] = [
            (ts, p) for ts, p in self.requests[client_ip] if ts > one_minute_ago
        ]

        # Check limit for this path
        path_requests = len([p for _, p in self.requests[client_ip] if p == path])

        if path_requests >= limit:
            logger.warning(
                "⚠️  Rate limit exceeded for {client} on {path}",
                client=client_ip,
                path=path,
            )
            return Response(
                content="Too many requests",
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

    def set(self, key: str, value: Any, ttl_seconds: int = 60) -> None:
        """Store value with TTL."""
        self.cache[key] = (value, time.time() + ttl_seconds)

    def invalidate(self, pattern: str = "") -> None:
        """Invalidate cache entries matching pattern."""
        if not pattern:
            self.cache.clear()
        else:
            self.cache = {k: v for k, v in self.cache.items() if pattern not in k}


# Global cache instance
cache_store = CacheStore()


def cached(ttl_seconds: int = 60):
    """Decorator for caching async functions."""

    def decorator(func: Callable) -> Callable:
        async def wrapper(*args, **kwargs) -> Any:
            # Create cache key from function name and arguments
            cache_key = f"{func.__name__}:{str(args)}:{str(kwargs)}"
            cache_key = hashlib.md5(cache_key.encode()).hexdigest()

            # Check cache
            cached_value = cache_store.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Call function and cache result
            result = await func(*args, **kwargs)
            cache_store.set(cache_key, result, ttl_seconds)
            return result

        return wrapper

    return decorator


class BatchProcessor:
    """
    Batch multiple operations to reduce database calls.
    Useful for vote counting and statistics.
    """

    def __init__(self, batch_size: int = 100, flush_interval: float = 1.0):
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.batch: dict = {}
        self.last_flush = time.time()

    def add(self, key: str, value: Any) -> None:
        """Add item to batch."""
        if key not in self.batch:
            self.batch[key] = []
        self.batch[key].append(value)

        # Auto-flush if batch is full
        if sum(len(v) for v in self.batch.values()) >= self.batch_size:
            self.flush()

    def flush(self) -> dict:
        """Get and clear batch."""
        batch = self.batch.copy()
        self.batch.clear()
        self.last_flush = time.time()
        return batch

    def should_flush(self) -> bool:
        """Check if batch should be flushed (time-based)."""
        return (time.time() - self.last_flush) >= self.flush_interval


# Global batch processor
batch_processor = BatchProcessor()


class ConnectionPool:
    """
    Database connection pooling for high concurrency.
    Prevents connection exhaustion.
    """

    def __init__(self, min_size: int = 5, max_size: int = 20):
        self.min_size = min_size
        self.max_size = max_size
        self.connections: list = []
        self.available: list = []

    async def acquire(self):
        """Get connection from pool."""
        if self.available:
            return self.available.pop()
        if len(self.connections) < self.max_size:
            # Create new connection
            conn = None  # Placeholder - implement with actual DB
            self.connections.append(conn)
            return conn
        # Wait for available connection
        while not self.available:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
        return self.available.pop()

    async def release(self, conn):
        """Return connection to pool."""
        self.available.append(conn)
