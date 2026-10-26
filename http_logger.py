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

from config import access_logger, config


class RequestLogger(BaseHTTPMiddleware):
    """
    Middleware for clean HTTP request logging with filtering.

    Features:
    - Filters repeated requests within time window
    - Groups similar requests (ignoring numeric IDs)
    - Ignores health-check and static file endpoints
    - Shows only important information
    """

    # Paths that are logged but grouped (count duplicates within time window)
    GROUPED_PATTERNS = [
        r"^/api/vote/\d+$",
        r"^/api/proxies",
    ]

    # Max entries kept per grouped path before old ones roll off
    HISTORY_MAXLEN = 100

    def __init__(self, app):
        super().__init__(app)
        # Store request counts: {pattern_key: deque of (timestamp, status))}
        self.request_history: dict = defaultdict(
            lambda: deque(maxlen=self.HISTORY_MAXLEN)
        )

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
        if any(path.startswith(skip) for skip in config.http_log.skip_paths):
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
        group_window = config.http_log.group_window_seconds
        group_interval = config.http_log.group_count_interval

        # Check if this is a grouped path
        if self._is_grouped_path(path):
            normalized = self._normalize_path(path)
            key = (method, normalized, status)
            history = self.request_history[key]

            # Remove old entries outside the time window
            while history and (current_time - history[0][0]) > group_window:
                history.popleft()

            # Count recent similar requests
            count = len(history)
            history.append((current_time, status))

            # Log first request immediately, then every group_interval-th request
            if count == 0:
                self._log_message(method, normalized, client, status, duration)
            elif count % group_interval == group_interval - 1:
                self._log_message(
                    method,
                    normalized,
                    client,
                    status,
                    duration,
                    note=config.http_log.repeat_suffix.format(count=count + 1),
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
        max_path_length = config.http_log.max_path_length
        if len(path) > max_path_length:
            keep = max_path_length - len("...")
            path = path[:keep] + "..."

        message = config.http_log.message_format.format(
            method=method,
            path=path,
            status=status,
            duration_ms=f"{duration * 1000:.0f}",
            client=client,
        )
        if note:
            message += note

        if config.logging.access_log is True:
            access_logger.info(message)
