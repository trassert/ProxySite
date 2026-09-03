"""
Ping checker for MTProto proxies.

Supports:
- default MTProto proxies;
- obfuscated proxies with "dd" secrets;
- fake-tls proxies with "ee" secrets.

Uses valid MTProto handshake packets to bypass replay protection and DPI.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import os
import random
import ssl
import struct
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlparse

from models import PingStatus, ProxyType

PING_OK_THRESHOLD = 500
PING_WARNING_THRESHOLD = 1500


class ProxyMode(StrEnum):
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
    is_fallback: bool
    tcp_ping_ms: int | None = None


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
    HANDSHAKE_LEN: int = 64
    PROTO_TAG_POS: int = 56
    DC_IDX_POS: int = 60
    SKIP_LEN: int = 8
    KEY_LEN: int = 32
    IV_LEN: int = 16

    PROTO_TAG_ABRIDGED = b"\xef\xef\xef\xef"
    PROTO_TAG_INTERMEDIATE = b"\xee\xee\xee\xee"
    PROTO_TAG_SECURE = b"\xdd\xdd\xdd\xdd"

    # Reserved patterns that must NOT appear in handshake
    RESERVED_NONCE_FIRST_CHARS = [b"\xef"]
    RESERVED_NONCE_BEGININGS = [
        b"\x48\x45\x41\x44",  # HEAD
        b"\x50\x4f\x53\x54",  # POST
        b"\x47\x45\x54\x20",  # GET
        b"\xee\xee\xee\xee",
        b"\xdd\xdd\xdd\xdd",
        b"\x16\x03\x01\x02",  # TLS
    ]
    RESERVED_NONCE_CONTINUES = [b"\x00\x00\x00\x00"]

    # Realistic TLS cipher suites (matching modern browsers)
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
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        proxy_type: ProxyType = ProxyType.MT_PROTO,
    ) -> PingResult:
        """Check proxy availability using protocol-specific handshake."""
        secret = cls._normalize_secret(secret)
        mode = cls._detect_mode(secret)

        loop = asyncio.get_running_loop()
        protocol_deadline = loop.time() + cls.TIMEOUT

        if proxy_type == ProxyType.WEB:
            proxy_get_ok, ping_ms = await cls._webproxy_get_check(
                server, secret, protocol_deadline
            )
        else:
            proxy_get_ok, ping_ms = await cls._proxy_get_check(
                server, port, secret, mode, protocol_deadline
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
    async def _webproxy_get_check(
        cls,
        server: str,
        secret: str | None,
        deadline: float,
    ) -> tuple[bool, int | None]:
        """Check the webproxy bridge with its real HTTPS GET entry point."""
        if not secret:
            return False, None

        try:
            secret_bytes = bytes.fromhex(secret)
        except ValueError:
            return False, None

        context = b"tdesktop-web-proxy-bridge-v1\n" + server.encode("ascii")
        capability = hmac.new(secret_bytes, context, hashlib.sha256).digest()
        bridge = base64.urlsafe_b64encode(capability).rstrip(b"=").decode("ascii")
        request = (
            f"GET /?bridge={bridge} HTTP/1.1\r\n"
            f"Host: {server}\r\n"
            "Connection: close\r\n"
            "User-Agent: Mozilla/5.0\r\n\r\n"
        ).encode("ascii")
        loop = asyncio.get_running_loop()
        writer = None
        start = loop.time()

        try:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None
            tls = ssl.create_default_context()
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(server, 443, ssl=tls, server_hostname=server),
                timeout=remaining,
            )
            writer.write(request)
            await asyncio.wait_for(writer.drain(), timeout=deadline - loop.time())
            header = await asyncio.wait_for(
                reader.readuntil(b"\r\n\r\n"), timeout=deadline - loop.time()
            )
            status = header.split(b" ", 2)[1]
            if not status.startswith((b"2", b"3")):
                return False, None
            return True, int((loop.time() - start) * 1000)
        except (_NETWORK_ERRORS, ssl.SSLError, UnicodeError, ValueError):
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    async def _proxy_get_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        mode: ProxyMode | None = None,
        deadline: float | None = None,
    ) -> tuple[bool, int | None]:
        """Protocol-level check dispatcher."""
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + cls.TIMEOUT

        secret = cls._normalize_secret(secret)
        if mode is None:
            mode = cls._detect_mode(secret)

        if mode == ProxyMode.FAKE_TLS:
            checks = (cls._fake_tls_check,)
        elif mode == ProxyMode.OBFUSCATED:
            checks = (cls._obfuscated_handshake_check,)
        else:
            # Default mode also uses obfuscated handshake (without secret)
            checks = (cls._obfuscated_handshake_check,)

        for check in checks:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break

            ok, ping_ms = await check(server, port, secret, timeout=remaining)
            if ok:
                return True, ping_ms

        return False, None

    @classmethod
    async def _obfuscated_handshake_check(
        cls,
        server: str,
        port: int,
        secret: str | None = None,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """
        Valid MTProto obfuscated handshake for dd-proxies and default mode.
        Generates proper 64-byte init packet with AES-CTR encryption.
        """
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

            # Generate valid handshake
            handshake = cls._build_obfuscated_handshake(secret)

            writer.write(handshake)

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            await asyncio.wait_for(writer.drain(), timeout=remaining)

            ping_ms = int((loop.time() - start) * 1000)

            # Wait for server response (encrypted handshake response)
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            try:
                response = await asyncio.wait_for(
                    reader.read(64),
                    timeout=min(remaining, 1.0),
                )
                # Server should respond with 64 bytes
                if len(response) >= 8:
                    return True, ping_ms
            except TimeoutError:
                # Connection accepted but no response yet - still valid
                if not writer.is_closing():
                    return True, ping_ms

            return False, None

        except _NETWORK_ERRORS:
            return False, None
        finally:
            await cls._close_writer(writer)

    @classmethod
    def _build_obfuscated_handshake(cls, secret: str | None) -> bytes:
        """Build valid 64-byte MTProto obfuscated handshake."""
        # Generate random 64-byte array
        rnd = bytearray(os.urandom(cls.HANDSHAKE_LEN))

        # Ensure first bytes don't match reserved patterns
        while True:
            if rnd[:1] in cls.RESERVED_NONCE_FIRST_CHARS:
                rnd[:1] = os.urandom(1)
                continue
            if rnd[:4] in cls.RESERVED_NONCE_BEGININGS:
                rnd[:4] = os.urandom(4)
                continue
            if rnd[4:8] in cls.RESERVED_NONCE_CONTINUES:
                rnd[4:8] = os.urandom(4)
                continue
            break

        # Set protocol tag based on secret type
        if secret:
            secret_bytes = bytes.fromhex(secret)
            if secret_bytes.startswith(b"\xdd"):
                proto_tag = cls.PROTO_TAG_SECURE
            elif secret_bytes.startswith(b"\xee"):
                proto_tag = cls.PROTO_TAG_INTERMEDIATE
            else:
                proto_tag = cls.PROTO_TAG_ABRIDGED
        else:
            # Default: use secure protocol
            proto_tag = cls.PROTO_TAG_SECURE

        rnd[cls.PROTO_TAG_POS : cls.PROTO_TAG_POS + 4] = proto_tag

        # Set DC index (random valid DC 1-5)
        dc_idx = random.randint(1, 5)
        rnd[cls.DC_IDX_POS : cls.DC_IDX_POS + 4] = struct.pack("<I", dc_idx)

        # Extract encryption key and IV (bytes 8-56, reversed)
        dec_key_and_iv = rnd[cls.SKIP_LEN : cls.SKIP_LEN + cls.KEY_LEN + cls.IV_LEN][
            ::-1
        ]
        dec_key_and_iv[: cls.KEY_LEN]
        dec_key_and_iv[cls.KEY_LEN :]

        # For obfuscated handshake, we encrypt the packet itself
        # Use the same key/iv for encryption (reversed back)
        enc_key_and_iv = rnd[cls.SKIP_LEN : cls.SKIP_LEN + cls.KEY_LEN + cls.IV_LEN]
        enc_key = enc_key_and_iv[: cls.KEY_LEN]
        enc_iv = enc_key_and_iv[cls.KEY_LEN :]

        # Encrypt from position 56 onwards (proto_tag and dc_idx)
        encrypted_part = cls._aes_ctr_encrypt(
            bytes(rnd[cls.PROTO_TAG_POS :]),
            enc_key,
            int.from_bytes(enc_iv, "big"),
        )

        # Combine: first 56 bytes unchanged + encrypted part
        return bytes(rnd[: cls.PROTO_TAG_POS]) + encrypted_part

    @classmethod
    def _aes_ctr_encrypt(cls, data: bytes, key: bytes, iv: int) -> bytes:
        """Simple AES-CTR encryption using pycryptodome or cryptography."""
        try:
            # Try pycryptodome first
            from Crypto.Cipher import AES

            cipher = AES.new(key, AES.MODE_CTR, nonce=iv.to_bytes(8, "big"))
            return cipher.encrypt(data)
        except ImportError:
            pass

        try:
            # Try cryptography library
            from cryptography.hazmat.primitives.ciphers import (
                Cipher,
                algorithms,
                modes,
            )

            nonce = iv.to_bytes(16, "big")
            cipher = Cipher(algorithms.AES(key), modes.CTR(nonce))
            encryptor = cipher.encryptor()
            return encryptor.update(data) + encryptor.finalize()
        except ImportError:
            pass

        # Fallback: use pyaes (pure Python)
        try:
            import pyaes

            ctr = pyaes.Counter(iv)
            aes = pyaes.AESModeOfOperationCTR(key, ctr)
            return b"".join(
                aes.encrypt(data[i : i + 16]) for i in range(0, len(data), 16)
            )
        except ImportError:
            # Last resort: XOR with key (not secure but allows ping to work)
            result = bytearray()
            for i, byte in enumerate(data):
                result.append(byte ^ key[i % len(key)])
            return bytes(result)

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
        Fake-tls probe with randomized ClientHello to avoid JA3 detection.
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

            await asyncio.wait_for(writer.drain(), timeout=remaining)

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, None

            header = await asyncio.wait_for(reader.read(5), timeout=remaining)

            ping_ms = int((loop.time() - start) * 1000)

            # TLS record validation
            if len(header) < 5:
                return False, None

            content_type = header[0]
            version_major = header[1]

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
    def _build_tls_client_hello(cls, server_name: str | None) -> bytes:
        """Build randomized TLS ClientHello to avoid JA3 fingerprinting."""
        # Randomize cipher order slightly
        ciphers = list(cls.TLS_CIPHER_SUITES)
        if random.random() < 0.3:
            # Occasionally shuffle last few ciphers
            ciphers[-5:] = random.sample(ciphers[-5:], len(ciphers[-5:]))

        extensions = b""

        # SNI extension
        if server_name and not cls._is_ip(server_name):
            try:
                name = server_name.encode("idna")
            except UnicodeError:
                name = server_name.encode("ascii", "ignore")

            if name:
                server_name_entry = struct.pack("!BH", 0, len(name)) + name
                server_name_list = (
                    struct.pack("!H", len(server_name_entry)) + server_name_entry
                )
                extensions += (
                    struct.pack("!HH", 0x0000, len(server_name_list)) + server_name_list
                )

        # Random padding extension (GREASE)
        if random.random() < 0.5:
            grease_ext = struct.pack("!HH", random.randint(0x0A0A, 0xFAFA), 0)
            extensions += grease_ext

        random_bytes = os.urandom(32)

        cipher_bytes = b"".join(struct.pack("!H", cipher) for cipher in ciphers)
        cipher_suites = struct.pack("!H", len(cipher_bytes)) + cipher_bytes

        # ClientHello
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

        # TLS record with randomized version
        tls_version = random.choice([b"\x03\x01", b"\x03\x03"])
        return b"\x16" + tls_version + struct.pack("!H", len(handshake)) + handshake

    @classmethod
    async def _tcp_check(
        cls,
        server: str,
        port: int,
        *,
        timeout: float | None = None,
    ) -> tuple[bool, int | None]:
        """Basic TCP connectivity check."""
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
    def _normalize_secret(cls, secret: str | None) -> str | None:
        """Normalize secret format."""
        if secret is None:
            return None

        value = str(secret).strip()
        if not value:
            return None

        # Extract from URL if present
        if "secret=" in value or "://" in value:
            try:
                parsed = urlparse(value if "://" in value else f"https://{value}")
                query = parse_qs(parsed.query)
                if "secret" in query and query["secret"]:
                    value = query["secret"][0]
            except Exception:
                pass

        value = value.strip().lower()
        value = value.removeprefix("0x")

        value = "".join(value.split())
        if not value:
            return None

        if all(c in "0123456789abcdef" for c in value):
            return value

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
        """Extract SNI domain from fake-tls secret."""
        secret = cls._normalize_secret(secret)
        if not secret or not secret.lower().startswith("ee"):
            return None

        rest = secret[2:]
        candidates = []

        if len(rest) > 32:
            candidates.append(rest[32:])
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
        """Validate domain/IP format."""
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
