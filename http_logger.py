"""
HTTP Request Logger Middleware for FastAPI.
Provides clean, filtered logging of HTTP requests without spam.
"""

import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from config import logger


class RequestLogger(BaseHTTPMiddleware):
    """
    Middleware for clean HTTP request logging with filtering.

    Features:
    - Filters repeated requests within time window
    - Groups similar requests
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
    GROUPED_PATHS = {
        "/api/vote/",
        "/api/proxies",
    }

    # Time window (seconds) for grouping similar requests
    GROUP_WINDOW = 5

    def __init__(self, app):
        super().__init__(app)
        # Store request counts: {(method, path): deque of (timestamp, status))}
        self.request_history: dict = defaultdict(lambda: deque(maxlen=100))

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

    def _log_request(
        self,
        method: str,
        path: str,
        client: str,
        status: int,
        duration: float,
    ) -> None:
        """Log HTTP request with smart filtering."""
        key = (method, path)
        current_time = time.time()

        # Check if this is a grouped path
        is_grouped = any(
            path.startswith(grouped) for grouped in self.GROUPED_PATHS
        )

        if is_grouped:
            # For grouped paths, check if we've already logged similar request recently
            history = self.request_history[key]

            # Remove old entries outside the time window
            while (
                history and (current_time - history[0][0]) > self.GROUP_WINDOW
            ):
                history.popleft()

            # Count recent similar requests
            count = len(history)
            history.append((current_time, status))

            # Only log detailed info for the first request in the group
            if count == 0:
                self._write_log(method, path, client, status, duration)
            elif count == 4:  # Log every 5th similar request
                self._write_log(
                    method,
                    path,
                    client,
                    status,
                    duration,
                    note=f"(+{count} similar in {self.GROUP_WINDOW}s)",
                )
        else:
            # For non-grouped paths, always log
            self._log_request_detailed(method, path, client, status, duration)

    def _log_request_detailed(
        self,
        method: str,
        path: str,
        client: str,
        status: int,
        duration: float,
    ) -> None:
        """Detailed logging for non-grouped requests."""
        # Truncate long paths
        display_path = path[:60] + "..." if len(path) > 60 else path

        status_emoji = (
            "✓" if 200 <= status < 300 else "⚠" if 300 <= status < 400 else "✗"
        )

        logger.info(
            "{emoji} {method:6} {path:65} {status:3} ({duration:.2f}s) | {client}",
            emoji=status_emoji,
            method=method,
            path=display_path,
            status=status,
            duration=duration,
            client=client,
        )

    def _write_log(
        self,
        method: str,
        path: str,
        client: str,
        status: int,
        duration: float,
        note: str = "",
    ) -> None:
        """Write log message."""
        # Truncate long paths
        display_path = path[:50] + "..." if len(path) > 50 else path

        status_emoji = (
            "✓" if 200 <= status < 300 else "⚠" if 300 <= status < 400 else "✗"
        )

        message = (
            f"{status_emoji} {method:6} {display_path:53} "
            f"{status:3} ({duration * 1000:.0f}ms) | {client}"
        )

        if note:
            message += f" {note}"

        logger.info(message)


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
