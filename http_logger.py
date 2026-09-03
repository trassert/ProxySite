"""
HTTP Request Logger Middleware for FastAPI.
Provides clean, filtered logging of HTTP requests without spam.
"""

import re
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from config import access_logger, logger


class RequestLogger(BaseHTTPMiddleware):
    """
    Middleware for clean HTTP request logging with filtering.

    Features:
    - Filters repeated requests within time window
    - Groups similar requests (ignoring numeric IDs)
    - Ignores health-check and static file endpoints
    - Shows only important information
    """

    # Endpoints to skip logging (health checks, static files, etc.)
    SKIP_PATHS = {
        "/static/",
        "/health",
        "/metrics",
        "/ping",
    }

    # Paths that are logged but grouped (count duplicates within time window)
    GROUPED_PATTERNS = [
        r"^/api/vote/\d+$",
        r"^/api/proxies",
    ]

    # Count interval and time window for grouping similar requests
    GROUP_COUNT_INTERVAL = 10
    GROUP_WINDOW = 10

    def __init__(self, app):
        super().__init__(app)
        # Store request counts: {pattern_key: deque of (timestamp, status))}
        self.request_history: dict = defaultdict(lambda: deque(maxlen=100))

    @staticmethod
    def _normalize_path(path: str) -> str:
        """Normalize path by replacing numeric IDs with placeholder."""
        # Replace numeric IDs with * for grouping
        return re.sub(r"/\d+(?=/|$)", "/*", path)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Process request and response."""
        path = request.url.path
        method = request.method

        # Skip logging for certain paths
        if any(path.startswith(skip) for skip in self.SKIP_PATHS):
            return await call_next(request)

        # Get client info
        client_host = request.client.host if request.client else "unknown"

        # Time the request
        start_time = time.time()
        response = await call_next(request)
        duration = time.time() - start_time

        # Extract status code
        status_code = response.status_code

        # Log the request
        self._log_request(
            method=method,
            path=path,
            client=client_host,
            status=status_code,
            duration=duration,
        )

        return response

    def _is_grouped_path(self, path: str) -> bool:
        """Check if path should be grouped."""
        return bool(
            any(re.match(pattern, path) for pattern in self.GROUPED_PATTERNS)
            or re.search(r"/\d+(?=/|$)", path)
        )

    def _log_request(
        self,
        method: str,
        path: str,
        client: str,
        status: int,
        duration: float,
    ) -> None:
        """Log HTTP request with smart filtering."""
        current_time = time.time()

        # Check if this is a grouped path
        if self._is_grouped_path(path):
            normalized = self._normalize_path(path)
            key = (method, normalized, status)
            history = self.request_history[key]

            # Remove old entries outside the time window
            while (
                history and (current_time - history[0][0]) > self.GROUP_WINDOW
            ):
                history.popleft()

            # Count recent similar requests
            count = len(history)
            history.append((current_time, status))

            # Log first request immediately, then every GROUP_COUNT_INTERVAL-th request
            if count == 0:
                self._log_message(method, normalized, client, status, duration)
            elif (
                count % self.GROUP_COUNT_INTERVAL
                == self.GROUP_COUNT_INTERVAL - 1
            ):
                self._log_message(
                    method,
                    normalized,
                    client,
                    status,
                    duration,
                    note=f" +{count + 1}x",
                )
        else:
            # For non-grouped paths, always log
            self._log_message(method, path, client, status, duration)

    def _log_message(
        self,
        method: str,
        path: str,
        client: str,
        status: int,
        duration: float,
        note: str = "",
    ) -> None:
        """Write formatted log message."""
        # Truncate long paths
        if len(path) > 40:
            path = path[:37] + "..."

        # Format duration in ms
        duration_ms = duration * 1000

        message = f"{method} {path} {status} {duration_ms:.0f}ms {client}"
        if note:
            message += note

        access_logger.info(message)


class SilentUvicornLogger:
    """
    Custom logger for Uvicorn that silences access logs.
    Replace Uvicorn's default logging with this.
    """

    def __init__(self):
        self.logger = logger

    def access(self, request, response, process_time):
        """Silence access logging - use RequestLogger middleware instead."""
        pass

    def error(self, message):
        """Log errors."""
        self.logger.error(message)

    def critical(self, message):
        """Log critical messages."""
        self.logger.critical(message)

    def warning(self, message):
        """Log warnings."""
        self.logger.warning(message)

    def info(self, message):
        """Log info messages."""
        # Filter out access logs
        if "GET" not in message and "POST" not in message:
            self.logger.info(message)

    def debug(self, message):
        """Log debug messages."""
        self.logger.debug(message)

    def trace(self, message):
        """Log trace messages."""
        self.logger.trace(message)
