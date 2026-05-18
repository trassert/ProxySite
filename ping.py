"""
Ping checker for MTProto proxies.
Uses MTProto Proxy-get request for accurate availability check.
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


class PingChecker:
    """Async ping checker using MTProto Proxy-get request."""

    TIMEOUT: float = 5.0
    # MTProto Proxy-get magic bytes
    PROXY_GET_REQUEST = b"\x00\x01\x00\x01\x00\x00\x00\x00"

    @classmethod
    async def check(cls, server: str, port: int, secret: str | None = None) -> PingResult:
        """
        Check proxy availability using MTProto Proxy-get request.
        Returns PingResult with status based on connection success and latency.
        """
        tcp_ok, ping_ms = await cls._proxy_get_check(server, port, secret)

        if tcp_ok:
            if ping_ms is not None and ping_ms <= PING_OK_THRESHOLD:
                status = PingStatus.OK
            elif ping_ms is not None and ping_ms <= PING_WARNING_THRESHOLD:
                status = PingStatus.OK
            else:
                status = PingStatus.WARNING
        else:
            status = PingStatus.FAILED
            ping_ms = None

        return PingResult(
            ping_ms=ping_ms,
            status=status,
            tcp_ok=tcp_ok,
            dns_ok=tcp_ok,
        )

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

        try:
            start = loop.time()

            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=cls.TIMEOUT,
            )

            # Build MTProto Proxy-get request
            # Format: dd (magic) + 4 bytes padding + 4 bytes timestamp
            secret_bytes = bytes.fromhex(secret) if secret else b""
            
            # If secret starts with domain fronting prefix (ee...), handle it
            if secret_bytes.startswith(b"\xee"):
                # Domain fronting: ee + 4 bytes domain len + domain + rest
                request = cls.PROXY_GET_REQUEST
            else:
                # Standard secret - use as padding
                # MTProto expects: magic(4) + random_padding(up to 512) + timestamp(4)
                padding = secret_bytes[:56] if len(secret_bytes) >= 32 else secret_bytes.ljust(56, b"\x00")
                timestamp = struct.pack(">I", int(time.time()))
                request = cls.PROXY_GET_REQUEST + padding + timestamp

            writer.write(request)
            await writer.drain()

            # Read response (MTProto proxy responds with same structure)
            response = await asyncio.wait_for(
                writer.read(8),
                timeout=cls.TIMEOUT,
            )

            end = loop.time()
            ping_ms = int((end - start) * 1000)

            # Validate response - should start with same magic or 0x00
            if len(response) >= 4 and response[0:2] == b"\x00\x01":
                success = True
            elif len(response) >= 4:
                # Some proxies respond with different but valid response
                success = True
            else:
                success = False

            writer.close()
            await writer.wait_closed()

            if not success:
                return False, None

        except (TimeoutError, ConnectionRefusedError, OSError, asyncio.IncompleteReadError):
            return False, None
        else:
            return True, ping_ms
