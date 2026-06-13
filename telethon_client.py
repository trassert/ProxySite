"""Telegram listener for configured public channels.

This module starts a Telethon client and listens for new messages in
configured channels. Any tg://proxy or https://t.me/proxy links found in
message text are parsed and added to the local proxy database.
"""

from pathlib import Path

from loguru import logger
from telethon import TelegramClient, events

from config import config
from database import db
from parser import ProxyLinkParser

SESSION_DIR = Path(".session")
SESSION_DIR.mkdir(parents=True, exist_ok=True)
SESSION_FILE = SESSION_DIR / f"{config.telegram.session_name}.session"


class TelegramProxyListener:
    """Manages Telethon client lifecycle and message parsing."""

    client: TelegramClient | None = None
    _started: bool = False

    @classmethod
    async def start(cls) -> None:
        if cls._started:
            logger.info("Telegram listener already running")
            return

        if not config.telegram.enabled:
            logger.info("Telegram listener disabled in config")
            return

        if not config.telegram.api_id or not config.telegram.api_hash:
            msg = "Telegram api_id/api_hash must be set in config.toml"
            raise RuntimeError(msg)

        cls.client = TelegramClient(
            str(SESSION_FILE),
            config.telegram.api_id,
            config.telegram.api_hash,
        )

        @cls.client.on(events.NewMessage(chats=config.telegram.channels))
        async def _on_new_message(event: events.NewMessage.Event) -> None:
            try:
                text = event.raw_text or ""
                proxies, errors = ProxyLinkParser.parse_text(text)
                if proxies:
                    added = 0
                    for proxy in proxies:
                        result = await db.add_proxy(proxy)
                        if result:
                            added += 1
                    logger.info(
                        "Telegram message imported {count} proxies from {chat}",
                        count=added,
                        chat=str(event.chat_id),
                    )
                if errors:
                    logger.warning(
                        "Telegram message parse warnings: {errors}",
                        errors=errors,
                    )
            except Exception as exc:
                logger.exception(
                    "Failed to process Telegram message: {error}", error=exc
                )

        await cls.client.start()
        cls._started = True
        logger.info("Telegram proxy listener started")

    @classmethod
    async def stop(cls) -> None:
        if not cls._started or cls.client is None:
            return

        await cls.client.disconnect()
        cls._started = False
        logger.info("Telegram proxy listener stopped")
