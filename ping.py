"""
Ping checker for MTProto proxies.
Uses TCP connect only for reliability.
"""

import asyncio
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
    """Async ping checker using TCP connect only."""

    TIMEOUT: float = 5.0

    @classmethod
    async def check(cls, server: str, port: int) -> PingResult:
        """
        Check proxy availability using TCP connect.
        Returns PingResult with status based on connection success and latency.
        """
        tcp_ok, ping_ms = await cls._tcp_check(server, port)

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
    async def _tcp_check(cls, server: str, port: int) -> tuple[bool, int | None]:
        """
        Perform TCP connect check.
        Returns (success, time_ms).
        """
        loop = asyncio.get_event_loop()

        try:
            start = loop.time()

            _, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=cls.TIMEOUT,
            )

            end = loop.time()
            ping_ms = int((end - start) * 1000)

            writer.close()
            await writer.wait_closed()

        except (TimeoutError, ConnectionRefusedError, OSError):
            return False, None
        else:
            return True, ping_ms
