"""
Ping checker for MTProto proxies.

Supports:
- default MTProto proxies;
- obfuscated proxies with "dd" secrets;
- fake-tls proxies with "ee" secrets.

Uses protocol-specific probes first and falls back to basic TCP check.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
import struct
import time
from dataclasses import dataclass
from enum import Enum
from urllib.parse import parse_qs, urlparse

from models import PingStatus

PING_OK_THRESHOLD = 500
PING_WARNING_THRESHOLD = 1500


class ProxyMode(str, Enum):
    DEFAULT = "default"
    OBFUSCATED = "obfuscated"
    FAKE_TLS = "fake_tls"


@dataclass
class PingResult:
    """Result of ping check."""

    ping_ms: int | None
    status: PingStatus
    tcp_ok: bool
    dns_ok: bool
    is_fallback: bool  # True if TCP fallback was used
    tcp_ping_ms: int | None = None  # TCP ping time when fallback is used


_NETWORK_ERRORS = (
    TimeoutError,
    asyncio.TimeoutError,
    ConnectionRefusedError,
    OSError,
    asyncio.IncompleteReadError,
)


class PingChecker:
    """Async ping checker for default, obfuscated and fake-tls MTProto proxies."""

    TIMEOUT: float = 5.0

    # If an obfuscated proxy accepts connection but does not answer immediately,
    # wait this long before treating connection as alive.
    OBFUSCATED_NO_RESPONSE_TIMEOUT: float = 1.0

    # Legacy request kept for backward compatibility and default proxies.
    PROXY_GET_REQUEST = b"\x00\x01\x00\x01\x00\x00\x00\x00"

    # Common cipher suites for fake-tls ClientHello.
    TLS_CIPHER_SUITES = (
        0x1301,  # TLS_AES_128_GCM_SHA256
        0x1302,  # TLS_AES_256_GCM_SHA384
        0x1303,  # TLS_CHACHA20_POLY1305_SHA256
        0xC02B,  # TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256
        0xC02F,  # TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256
        0xC02C,  # TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384
        0xC030,  # TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384
        0xCCA9,  # TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256
        0xCCA8,  # TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256
        0x009C,  # TLS_RSA_WITH_AES_128_GCM_SHA256
        0x009D,  # TLS_RSA_WITH_AES_256_GCM_SHA384
        0x002F,  # TLS_RSA_WITH_AES_128_CBC_SHA
        0x0035,  # TLS_RSA_WITH_AES_256_CBC_SHA
        0x000A,  # TLS_RSA_WITH_3DES_EDE_CBC_SHA
    )

    @classmethod
    async def check(
        cls, server: str, port: int, secret: str | None = None
    ) -> PingResult:
        """
        Check proxy availability using the best protocol probe for secret type.
        Falls back to basic TCP check if protocol probe fails.
        """
        secret = cls._normalize_secret(secret)
        mode = cls._detect_mode(secret)

        loop = asyncio.get_running_loop()
        protocol_deadline = loop.time() + cls.TIMEOUT

        proxy_get_ok, ping_ms = await cls._proxy_get_check(
            server,
            port,
            secret,
            mode,
            protocol_deadline,
        )

        if proxy_get_ok:
            return cls._protocol_success(ping_ms)

        tcp_ok, tcp_ping_ms = await cls._tcp_check(server, port, timeout=cls.TIMEOUT)
        if tcp_ok:
            return cls._fallback_success(tcp_ping_ms)

        return PingResult(
            ping_ms=None,
            status=PingStatus.FAILED,
            tcp_ok=False,
            dns_ok=False,
            is_fallback=False,
        )

    @classmethod
    async def _proxy_get_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        mode: ProxyMode | None = None,
        deadline: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Protocol-level check dispatcher.

        Kept for backward compatibility. Old signature:
            _proxy_get_check(server, port, secret)
        still works.
        """
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + cls.TIMEOUT

        secret = cls._normalize_secret(secret)
        if mode is None:
            mode = cls._detect_mode(secret)

        if mode == ProxyMode.FAKE_TLS:
            checks = (
                cls._fake_tls_check,
                cls._legacy_proxy_get_check,
            )
        elif mode == ProxyMode.OBFUSCATED:
            checks = (
                cls._obfuscated_check,
                cls._legacy_proxy_get_check,
            )
        else:
            checks = (cls._legacy_proxy_get_check,)

        for check in checks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            ok, ping_ms = await check(server, port, secret, timeout=remaining)
            if ok:
                return True, ping_ms

        return False, None

    @classmethod
    async def _fake_tls_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Fake-tls probe.

        Parses domain from an ee-secret and sends a minimal TLS ClientHello.
        A valid TLS record from server is treated as protocol-level success.
        """
        try:
            domain = cls._extract_tls_domain(secret)
            if not domain and not cls._is_ip(server):
                domain = server

            hello = cls._build_tls_client_hello(domain)
        except Exception:
            return False, None

        loop = asyncio.get_running_loop()
        deadline = cls._deadline(timeout)
        start = loop.time()
        writer = None

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=remaining,
            )

            writer.write(hello)

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            await asyncio.wait_for(
                writer.drain(),
                timeout=max(0.001, remaining),
            )

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            header = await asyncio.wait_for(
                reader.read(5),
                timeout=remaining,
            )

            ping_ms = int((loop.time() - start) * 1000)

            # TLS record header:
            # type, version_major, version_minor, length(2)
            if len(header) < 5:
                return False, None

            content_type = header[0]
            version_major = header[1]

            # Accept handshake or alert records.
            if version_major != 0x03 or content_type not in (0x15, 0x16):
                return False, None

            return True, ping_ms

        except _NETWORK_ERRORS:
            return False, None
        except (ValueError, UnicodeError, struct.error):
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    async def _obfuscated_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Obfuscated dd-proxy probe.

        Full MTProto handshake without Telegram-side keys is not always possible,
        so this is a heuristic probe:
        - if server answers, OK;
        - if server accepts connection and does not reset it immediately, OK.
        """
        payload = cls._build_obfuscated_request(secret)

        loop = asyncio.get_running_loop()
        deadline = cls._deadline(timeout)
        start = loop.time()
        writer = None

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=remaining,
            )

            writer.write(payload)

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            await asyncio.wait_for(
                writer.drain(),
                timeout=max(0.001, remaining),
            )

            # Measure ping right after successful connect + write.
            ping_ms = int((loop.time() - start) * 1000)

            remaining = deadline - loop.time()
            wait_timeout = min(cls.OBFUSCATED_NO_RESPONSE_TIMEOUT, remaining)

            if wait_timeout <= 0:
                if writer.is_closing():
                    return False, None
                return True, ping_ms

            try:
                data = await asyncio.wait_for(
                    reader.read(8),
                    timeout=wait_timeout,
                )
            except (TimeoutError, asyncio.TimeoutError):
                # No immediate answer, but connection is still alive.
                if writer.is_closing():
                    return False, None
                return True, ping_ms

            if data:
                return True, ping_ms

            # EOF: server closed connection immediately.
            return False, None

        except _NETWORK_ERRORS:
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    async def _legacy_proxy_get_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Old/default proxy-get check.

        Kept for backward compatibility and default secrets.
        """
        request = cls._build_legacy_request(secret)
        if request is None:
            return False, None

        loop = asyncio.get_running_loop()
        deadline = cls._deadline(timeout)
        start = loop.time()
        writer = None

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=remaining,
            )

            writer.write(request)

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            await asyncio.wait_for(
                writer.drain(),
                timeout=max(0.001, remaining),
            )

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            response = await asyncio.wait_for(
                reader.read(8),
                timeout=remaining,
            )

            ping_ms = int((loop.time() - start) * 1000)

            # Old behavior: any 4+ bytes response is treated as success.
            if len(response) >= 4:
                return True, ping_ms

            return False, None

        except _NETWORK_ERRORS:
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    async def _tcp_check(
        cls,
        server: str,
        port: int,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Basic TCP connectivity check with ping measurement.
        """
        loop = asyncio.get_running_loop()
        deadline = cls._deadline(timeout)
        writer = None

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            start = loop.time()

            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, port),
                timeout=remaining,
            )

            ping_ms = int((loop.time() - start) * 1000)
            return True, ping_ms

        except _NETWORK_ERRORS:
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    def _build_legacy_request(cls, secret: str | None) -> bytes | None:
        """Build legacy proxy-get request compatible with old behavior."""
        if not secret:
            return cls.PROXY_GET_REQUEST

        try:
            secret_bytes = bytes.fromhex(secret)
        except ValueError:
            return None

        if secret_bytes.startswith(b"\xee"):
            return cls.PROXY_GET_REQUEST

        padding = (
            secret_bytes[:56]
            if len(secret_bytes) >= 32
            else secret_bytes.ljust(56, b"\x00")
        )
        timestamp = struct.pack(">I", int(time.time()))

        return cls.PROXY_GET_REQUEST + padding + timestamp

    @classmethod
    def _build_obfuscated_request(cls, secret: str | None) -> bytes:
        """Build a 64-byte obfuscated probe for dd-proxies."""
        raw = b""

        if secret:
            with contextlib.suppress(ValueError):
                raw = bytes.fromhex(secret)

        if raw.startswith(b"\xdd") and len(raw) >= 4:
            payload = raw
        else:
            payload = b"\xdd\xdd\xdd\xdd" + raw

        if len(payload) < 64:
            payload += os.urandom(64 - len(payload))

        return payload[:64]

    @classmethod
    def _build_tls_client_hello(cls, server_name: str | None) -> bytes:
        """Build a minimal TLS ClientHello with optional SNI."""
        extensions = b""

        if server_name and not cls._is_ip(server_name):
            try:
                name = server_name.encode("idna")
            except UnicodeError:
                name = server_name.encode("ascii", "ignore")

            if name:
                server_name_entry = struct.pack("!BH", 0, len(name)) + name
                server_name_list = struct.pack("!H", len(server_name_entry)) + server_name_entry
                extensions += struct.pack("!HH", 0x0000, len(server_name_list)) + server_name_list

        random_bytes = os.urandom(32)

        cipher_bytes = b"".join(
            struct.pack("!H", cipher) for cipher in cls.TLS_CIPHER_SUITES
        )
        cipher_suites = struct.pack("!H", len(cipher_bytes)) + cipher_bytes

        # ClientHello:
        # version, random, session_id_len=0, cipher_suites, compression_methods=null
        client_hello = (
            b"\x03\x03"
            + random_bytes
            + b"\x00"
            + cipher_suites
            + b"\x01\x00"
            + struct.pack("!H", len(extensions))
            + extensions
        )

        handshake = b"\x01" + struct.pack("!I", len(client_hello))[1:] + client_hello

        # TLS record: handshake, TLS 1.0 record version for compatibility.
        return b"\x16\x03\x01" + struct.pack("!H", len(handshake)) + handshake

    @classmethod
    def _normalize_secret(cls, secret: str | None) -> str | None:
        """Normalize secret: trim, lower, extract from tg/t.me links if present."""
        if secret is None:
            return None

        value = str(secret).strip()
        if not value:
            return None

        # Extract secret from links like tg://proxy?secret=... or t.me/proxy?secret=...
        if "secret=" in value or "://" in value:
            try:
                parsed = urlparse(value if "://" in value else f"https://{value}")
                query = parse_qs(parsed.query)
                if "secret" in query and query["secret"]:
                    value = query["secret"][0]
            except Exception:
                pass

        value = value.strip().lower()
        if value.startswith("0x"):
            value = value[2:]

        value = "".join(value.split())
        if not value:
            return None

        if all(c in "0123456789abcdef" for c in value):
            return value

        # If input is dirty but still contains a plausible hex ee/dd secret, extract it.
        filtered = "".join(c for c in value if c in "0123456789abcdef")
        if filtered.startswith(("dd", "ee")) and len(filtered) >= 34:
            return filtered

        return value

    @classmethod
    def _detect_mode(cls, secret: str | None) -> ProxyMode:
        """Detect proxy mode from secret prefix."""
        if not secret:
            return ProxyMode.DEFAULT

        secret = secret.lower()
        if secret.startswith("ee"):
            return ProxyMode.FAKE_TLS
        if secret.startswith("dd"):
            return ProxyMode.OBFUSCATED

        return ProxyMode.DEFAULT

    @classmethod
    def _extract_tls_domain(cls, secret: str | None) -> str | None:
        """
        Extract SNI domain from fake-tls secret.

        Common format:
            ee + 16 random bytes as hex + domain as hex
        """
        secret = cls._normalize_secret(secret)
        if not secret or not secret.lower().startswith("ee"):
            return None

        rest = secret[2:]
        candidates: list[str] = []

        # Standard fake-tls secret: ee + 32 hex chars random + domain hex.
        if len(rest) > 32:
            candidates.append(rest[32:])

        # Some simplified implementations may put domain directly after ee.
        candidates.append(rest)

        for candidate in candidates:
            if not candidate:
                continue

            try:
                raw = bytes.fromhex(candidate)
            except ValueError:
                continue

            for encoding in ("utf-8", "latin-1"):
                try:
                    domain = raw.decode(encoding).strip("\x00 \t\r\n")
                except UnicodeDecodeError:
                    continue

                if cls._looks_like_host(domain):
                    return domain

        return None

    @classmethod
    def _looks_like_host(cls, value: str) -> bool:
        """Heuristic validation for domain/IP extracted from secret."""
        if not value or len(value) < 3:
            return False

        if not value.isprintable() or any(ch.isspace() for ch in value):
            return False

        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            pass

        lowered = value.lower().strip(".")
        if "." not in lowered:
            return False

        return all(ch.isalnum() or ch in ".-_" for ch in lowered)

    @classmethod
    def _is_ip(cls, value: str) -> bool:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    @classmethod
    def _deadline(cls, timeout: float | None) -> float:
        loop = asyncio.get_running_loop()
        if timeout is None:
            return loop.time() + cls.TIMEOUT
        return loop.time() + max(0.0, float(timeout))

    @classmethod
    async def _close_writer(cls, writer: asyncio.StreamWriter | None) -> None:
        if writer is None:
            return

        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()

    @classmethod
    def _protocol_success(cls, ping_ms: int | None) -> PingResult:
        return PingResult(
            ping_ms=ping_ms,
            status=cls._status_for_ping(ping_ms),
            tcp_ok=True,
            dns_ok=True,
            is_fallback=False,
        )

    @classmethod
    def _fallback_success(cls, ping_ms: int | None) -> PingResult:
        return PingResult(
            ping_ms=ping_ms,
            status=PingStatus.WARNING,
            tcp_ok=True,
            dns_ok=False,
            is_fallback=True,
            tcp_ping_ms=ping_ms,
        )

    @classmethod
    def _status_for_ping(cls, ping_ms: int | None) -> PingStatus:
        if ping_ms is not None and ping_ms <= PING_OK_THRESHOLD:
            return PingStatus.OK
        return PingStatus.WARNING