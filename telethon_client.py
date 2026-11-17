"""Telegram listener for configured public channels.

This module starts a Telethon client and listens for new messages in
configured channels. Any supported Telegram proxy links found in message text
are parsed and added to the local proxy database.
"""

import asyncio
from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events

from config import config
from database import db
from models import PingStatus
from parser import ProxyLinkParser
from ping import PingChecker


class TelegramProxyListener:
    """Manages Telethon client lifecycle and message parsing."""

    client: TelegramClient | None = None
    _started: bool = False

    @classmethod
    async def start(cls) -> None:
        if cls._started:
            logger.info("Auto-scraper already running")
            return

        if not config.telegram.enabled:
            logger.info("Auto-scraper disabled in config")
            return

        if not config.telegram.api_id or not config.telegram.api_hash:
            msg = config.messages.telegram_credentials_required
            raise RuntimeError(msg)

        session_dir = Path(config.telegram.session_dir)
        await asyncio.to_thread(session_dir.mkdir, parents=True, exist_ok=True)
        session_file = session_dir / f"{config.telegram.session_name}.session"

        cls.client = TelegramClient(
            str(session_file),
            config.telegram.api_id,
            config.telegram.api_hash,
        )

        @cls.client.on(events.NewMessage(chats=config.telegram.channels))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            try:
                text = event.raw_text or ""
                # Proxy links live in inline URL buttons, not in the post text.
                for row in getattr(event.message.reply_markup, "rows", []):
                    for button in row.buttons:
                        url = getattr(button.type, "url", None)
                        if url:
                            text += f"\n{url}"
                proxies, errors = ProxyLinkParser.parse_text(text)
                try:
                    chat = await event.get_chat()
                    username = getattr(chat, "username", None)
                except Exception:
                    username = None
                source = (
                    f"https://t.me/{username}/{event.id}"
                    if username
                    else f"telegram://chat/{event.chat_id}/message/{event.id}"
                )
                if proxies:
                    added = 0
                    duplicates = 0
                    failed = 0
                    for proxy in proxies:
                        check = await PingChecker.check(
                            proxy.server,
                            proxy.port,
                            proxy.secret,
                            proxy.proxy_type,
                        )
                        if check.status == PingStatus.FAILED:
                            failed += 1
                            logger.warning(
                                "Proxy {address} failed ping check from {source}",
                                address=f"{proxy.server}:{proxy.port}",
                                source=source,
                            )
                            continue
                        result = await db.add_proxy(proxy)
                        if result:
                            added += 1
                            logger.info(
                                "Proxy {address} added from {source}",
                                address=f"{proxy.server}:{proxy.port}",
                                source=source,
                            )
                        else:
                            duplicates += 1
                    logger.info(
                        "Auto-scraper import from {source}: +{added} new, {duplicates} dup, {failed} failed, {total} links",
                        source=source,
                        added=added,
                        duplicates=duplicates,
                        failed=failed,
                        total=len(proxies),
                    )
                if errors:
                    logger.warning(
                        "Telegram message parse warnings: {count} invalid links from {source}",
                        count=len(errors),
                        source=source,
                    )
            except Exception as exc:
                logger.exception(
                    "Failed to process Telegram message: {error}", error=exc
                )

        await cls.client.start()
        cls._started = True
        logger.info(
            "Auto-scraper started: monitoring {channels} channel(s) via session {session}",
            channels=", ".join(config.telegram.channels),
            session=str(session_file),
        )

    @classmethod
    async def stop(cls) -> None:
        if not cls._started or cls.client is None:
            return

        await cls.client.disconnect()
        cls._started = False
        logger.info("Auto-scraper stopped")
