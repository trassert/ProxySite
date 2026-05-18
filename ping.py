"""
Ping checker for MTProto proxies.
Uses MTProto Proxy-get request for accurate availability check.
Falls back to basic TCP check if proxy-get fails.
"""

import asyncio
import struct
import time
from dataclasses import dataclass

from models import PingStatus

PING_OK_THRESHOLD = 500
PING_WARNING_THRESHOLD = 1500


@dataclass
class PingResult:
    """Result of ping check."""

    ping_ms: int | None
    status: PingStatus
    tcp_ok: bool
    dns_ok: bool
    is_fallback: bool  # True if TCP fallback was used
    tcp_ping_ms: int | None = None  # TCP ping time when fallback is used


class PingChecker:
    """Async ping checker using MTProto Proxy-get request with TCP fallback."""

    TIMEOUT: float = 5.0
    PROXY_GET_REQUEST = b"\x00\x01\x00\x01\x00\x00\x00\x00"

    @classmethod
    async def check(
        cls, server: str, port: int, secret: str | None = None
    ) -> PingResult:
        """
        Check proxy availability using MTProto Proxy-get request.
        Falls back to basic TCP check if proxy-get fails.
        Returns PingResult with status based on connection success and latency.
        """
        # First try proxy-get check
        proxy_get_ok, ping_ms = await cls._proxy_get_check(server, port, secret)

        if proxy_get_ok:
            # Proxy-get succeeded, use this result
            if ping_ms is not None and ping_ms <= PING_OK_THRESHOLD:
                status = PingStatus.OK
            elif ping_ms is not None and ping_ms <= PING_WARNING_THRESHOLD:
                status = PingStatus.OK
            else:
                status = PingStatus.WARNING
            return PingResult(
                ping_ms=ping_ms,
                status=status,
                tcp_ok=True,
                dns_ok=True,
                is_fallback=False,
            )

        # Proxy-get failed, try TCP fallback
        tcp_ok, tcp_ping_ms = await cls._tcp_check(server, port)

        if tcp_ok:
            # TCP connection succeeded but proxy-get failed
            status = PingStatus.WARNING  # Mark as warning since proxy-get failed
            return PingResult(
                ping_ms=tcp_ping_ms,  # Use TCP ping time
                status=status,
                tcp_ok=True,
                dns_ok=False,  # DNS/proxy protocol didn't work
                is_fallback=True,
                tcp_ping_ms=tcp_ping_ms,
            )

        # Both checks failed
        return PingResult(
            ping_ms=None,
            status=PingStatus.FAILED,
            tcp_ok=False,
            dns_ok=False,
            is_fallback=False,
        )

    @classmethod
    async def _tcp_check(cls, server: str, port: int) -> tuple[bool, int | None]:
        """
        Perform basic TCP connectivity check with ping measurement.
        Tries to establish a connection and measures the time.
        Returns (success, ping_ms).
        """
        loop = asyncio.get_event_loop()
        try:
            start = loop.time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=cls.TIMEOUT,
            )
            end = loop.time()
            ping_ms = int((end - start) * 1000)
            writer.close()
            await writer.wait_closed()
            return True, ping_ms
        except (TimeoutError, ConnectionRefusedError, OSError):
            return False, None

    @classmethod
    async def _proxy_get_check(
        cls, server: str, port: int, secret: str | None = None
    ) -> tuple[bool, int | None]:
        """
        Perform MTProto Proxy-get check.
        Sends a valid MTProto handshake request and waits for response.
        Returns (success, time_ms).
        """
        loop = asyncio.get_event_loop()
        reader = None
        writer = None
        try:
            start = loop.time()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=cls.TIMEOUT,
            )
            secret_bytes = bytes.fromhex(secret) if secret else b""
            if secret_bytes.startswith(b"\xee"):
                request = cls.PROXY_GET_REQUEST
            else:
                padding = (
                    secret_bytes[:56]
                    if len(secret_bytes) >= 32
                    else secret_bytes.ljust(56, b"\x00")
                )
                timestamp = struct.pack(">I", int(time.time()))
                request = cls.PROXY_GET_REQUEST + padding + timestamp
            writer.write(request)
            await writer.drain()
            response = await asyncio.wait_for(
                reader.readexactly(8),
                timeout=cls.TIMEOUT,
            )
            end = loop.time()
            ping_ms = int((end - start) * 1000)
            if len(response) >= 4 and response[0:2] == b"\x00\x01":
                success = True
            elif len(response) >= 4:
                success = True
            else:
                success = False
            writer.close()
            await writer.wait_closed()
            if not success:
                return False, None
        except (
            TimeoutError,
            ConnectionRefusedError,
            OSError,
            asyncio.IncompleteReadError,
        ):
            return False, None
        else:
            return True, ping_ms
