"""
Link parser for MTProto proxy URLs.
Supports tg://proxy and t.me/proxy formats.
"""

import re
from urllib.parse import parse_qs, urlparse

from models import ProxyBase


class ProxyLinkParser:
    """Parser for MTProto proxy links."""

    TG_PATTERN = re.compile(
        r"tg://proxy\?[^\s<>\"'\]\)]+",
        re.IGNORECASE,
    )
    HTTPS_PATTERN = re.compile(
        r"https?://t\.me/proxy\?[^\s<>\"'\]\)]+",
        re.IGNORECASE,
    )

    @classmethod
    def clean_link(cls, url: str) -> str:
        """
        Clean a proxy URL by removing trailing punctuation and artifacts
        that might be captured from messy text.
        """
        url = url.strip()

        while url and url[-1] in ".,;:!?)]'\"":
            url = url[:-1]
        return url

    @classmethod
    def parse_single(cls, url: str) -> ProxyBase | None:
        """
        Parse a single proxy URL.
        Returns ProxyBase or None if invalid.
        """
        url = cls.clean_link(url)

        if url.lower().startswith("tg://"):
            url = "https://t.me/" + url.split("://", 1)[1]

        try:
            parsed = urlparse(url)
            params = parse_qs(parsed.query)

            server = params.get("server", [None])[0]
            port_str = params.get("port", [None])[0]
            secret = params.get("secret", [None])[0]

            if not all([server, port_str, secret]):
                return None

            port = int(port_str)

            return ProxyBase(server=server, port=port, secret=secret)
        except (ValueError, TypeError):
            return None

    @classmethod
    def parse_text(cls, text: str) -> tuple[list[ProxyBase], list[str]]:
        """
        Extract all proxy links from text.
        Returns (list of valid proxies, list of error messages).
        """
        proxies: list[ProxyBase] = []
        errors: list[str] = []
        seen: set[tuple[str, int, str]] = set()

        tg_links = cls.TG_PATTERN.findall(text)
        https_links = cls.HTTPS_PATTERN.findall(text)

        all_links = tg_links + https_links

        for link in all_links:
            proxy = cls.parse_single(link)
            if proxy:
                key = (proxy.server, proxy.port, proxy.secret)
                if key not in seen:
                    seen.add(key)
                    proxies.append(proxy)
            else:
                errors.append(f"Invalid link: {link[:50]}...")

        return proxies, errors

    @classmethod
    def generate_link(
        cls, server: str, port: int, secret: str, format: str = "tg"
    ) -> str:
        """Generate proxy link in specified format."""
        if format == "https":
            return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"
        return f"tg://proxy?server={server}&port={port}&secret={secret}"
