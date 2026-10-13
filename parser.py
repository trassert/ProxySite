"""Link parser for MTProto and WEB proxy URLs."""

import re
from urllib.parse import parse_qs, urlparse

from config import config
from models import ProxyBase, ProxyType


class ProxyLinkParser:
    """Parser for MTProto proxy links."""

    TG_PATTERN = re.compile(
        r"tg://(?:proxy|webproxy)\?[^\s<>\"'\]\)]+",
        re.IGNORECASE,
    )
    HTTPS_PATTERN = re.compile(
        r"https?://t\.me/(?:proxy|webproxy)\?[^\s<>\"'\]\)]+",
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
            proxy_type = (
                ProxyType.WEB
                if parsed.path.lower() == "/webproxy"
                else ProxyType.MT_PROTO
            )

            if not all([server, secret]):
                return None

            if proxy_type == ProxyType.WEB:
                port = 443
            elif not port_str:
                return None
            else:
                port = int(port_str)

            return ProxyBase(
                server=server, port=port, secret=secret, proxy_type=proxy_type
            )
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
        seen: set[tuple[str, int, str, ProxyType]] = set()

        tg_links = cls.TG_PATTERN.findall(text)
        https_links = cls.HTTPS_PATTERN.findall(text)

        all_links = tg_links + https_links

        for link in all_links:
            proxy = cls.parse_single(link)
            if proxy:
                key = (proxy.server, proxy.port, proxy.secret, proxy.proxy_type)
                if key not in seen:
                    seen.add(key)
                    proxies.append(proxy)
            else:
                errors.append(config.messages.invalid_proxy_link)

        return proxies, errors
