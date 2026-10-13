"""Network security helpers (SSRF protection)."""

import asyncio
import ipaddress
import socket

from config import config


class BlockedHostError(ValueError):
    """Raised when a host does not resolve exclusively to public addresses."""


def _is_public(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True only for globally routable, non-special addresses."""
    return ip.is_global and not (ip.is_multicast or ip.is_reserved)


async def resolve_public_host(server: str) -> list[str]:
    """Resolve ``server`` and return the public IPs it may be reached at.

    Rejects literal IPs and hostnames that resolve to private, loopback,
    link-local, multicast or otherwise reserved ranges. Raises
    :class:`BlockedHostError` otherwise.

    Callers should connect to one of the returned IP literals (instead of the
    original hostname) so the validated address cannot be swapped for a private
    one by DNS rebinding between the check and the connection.
    """
    try:
        literal = ipaddress.ip_address(server)
    except ValueError:
        literal = None

    if literal is not None:
        if not _is_public(literal):
            raise BlockedHostError(config.messages.host_blocked)
        return [str(literal)]

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(server, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedHostError(config.messages.host_unresolved) from exc

    if not infos:
        raise BlockedHostError(config.messages.host_unresolved)

    ips: list[str] = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if not _is_public(ip):
            raise BlockedHostError(config.messages.host_blocked)
        value = str(ip)
        if value not in ips:
            ips.append(value)

    if not ips:
        raise BlockedHostError(config.messages.host_unresolved)
    return ips


async def ensure_public_host(server: str) -> None:
    """Ensure ``server`` resolves only to public addresses.

    Rejects literal IPs and hostnames that resolve to private, loopback,
    link-local, multicast or otherwise reserved ranges. Raises
    :class:`BlockedHostError` otherwise.
    """
    await resolve_public_host(server)
