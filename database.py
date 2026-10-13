"""
Database layer for MTProto Proxy Hub.
Async SQLite with aiosqlite.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from config import config
from models import PingStatus, ProxyBase, ProxyInDB, ProxyType, SortBy


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        """Return the active connection, raising if not connected."""
        if self._connection is None:
            msg = "Database not connected"
            raise RuntimeError(msg)
        return self._connection

    async def connect(self) -> None:
        """Initialize database connection and create tables."""
        path = self.db_path or Path(config.app.database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()
        await self._ensure_schema()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection is not None:
            await self._connection.close()
            self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection]:
        """Context manager for transactions."""
        conn = self.conn
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    async def _create_tables(self) -> None:
        """Create database tables if not exist."""
        await self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server TEXT NOT NULL,
                port INTEGER NOT NULL,
                secret TEXT NOT NULL,
                proxy_type TEXT DEFAULT 'mtproto',
                note TEXT,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                ping_ms INTEGER,
                tcp_ping_ms INTEGER,
                ping_status TEXT DEFAULT 'pending',
                tcp_ok INTEGER DEFAULT 0,
                dns_ok INTEGER DEFAULT 0,
                is_fallback INTEGER DEFAULT 0,
                pinned INTEGER DEFAULT 0,
                failed_since TEXT,
                created_at TEXT NOT NULL,
                last_checked TEXT,
                UNIQUE(server, port, secret)
            );
            CREATE INDEX IF NOT EXISTS idx_proxies_likes ON proxies(likes DESC);
            CREATE INDEX IF NOT EXISTS idx_proxies_dislikes ON proxies(dislikes DESC);
            CREATE INDEX IF NOT EXISTS idx_proxies_ping ON proxies(ping_ms);
            CREATE INDEX IF NOT EXISTS idx_proxies_created ON proxies(created_at DESC);
            CREATE TABLE IF NOT EXISTS votes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proxy_id INTEGER NOT NULL,
                voter_id TEXT NOT NULL,
                vote_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (proxy_id) REFERENCES proxies(id) ON DELETE CASCADE,
                UNIQUE(proxy_id, voter_id)
            );
            CREATE TABLE IF NOT EXISTS stats (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                last_cleanup TEXT
            );
            INSERT OR IGNORE INTO stats (id) VALUES (1);
        """)
        await self.conn.commit()

    async def _ensure_schema(self) -> None:
        """Add columns introduced by newer migrations and backfill failed_since."""
        conn = self.conn
        cursor = await conn.execute("PRAGMA table_info(proxies)")
        cols = {row[1] for row in await cursor.fetchall()}

        migrations = {
            "proxy_type": "ALTER TABLE proxies ADD COLUMN proxy_type TEXT DEFAULT 'mtproto'",
            "note": "ALTER TABLE proxies ADD COLUMN note TEXT",
            "tcp_ping_ms": "ALTER TABLE proxies ADD COLUMN tcp_ping_ms INTEGER",
            "failed_since": "ALTER TABLE proxies ADD COLUMN failed_since TEXT",
            "pinned": "ALTER TABLE proxies ADD COLUMN pinned INTEGER DEFAULT 0",
        }
        for column, statement in migrations.items():
            if column not in cols:
                with contextlib.suppress(Exception):
                    await conn.execute(statement)

        with contextlib.suppress(Exception):
            await conn.execute(
                """
                UPDATE proxies
                SET failed_since = last_checked
                WHERE ping_status = 'failed'
                  AND failed_since IS NULL
                  AND last_checked IS NOT NULL
                """
            )
        await conn.commit()

    def _row_to_proxy(self, row: aiosqlite.Row) -> ProxyInDB:
        """Convert database row to ProxyInDB model without re-validating stored values."""
        return ProxyInDB.model_construct(
            id=row["id"],
            server=row["server"],
            port=row["port"],
            secret=row["secret"],
            proxy_type=ProxyType(row["proxy_type"] or ProxyType.MT_PROTO),
            note=row["note"],
            likes=row["likes"],
            dislikes=row["dislikes"],
            ping_ms=row["ping_ms"],
            ping_status=PingStatus(row["ping_status"]),
            tcp_ok=bool(row["tcp_ok"]),
            dns_ok=bool(row["dns_ok"]),
            is_fallback=bool(row["is_fallback"]),
            pinned=bool(row["pinned"]),
            tcp_ping_ms=row["tcp_ping_ms"],
            failed_since=datetime.fromisoformat(row["failed_since"])
            if row["failed_since"]
            else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            last_checked=datetime.fromisoformat(row["last_checked"])
            if row["last_checked"]
            else None,
        )

    async def update_ping(
        self,
        proxy_id: int,
        ping_ms: int | None,
        ping_status: PingStatus,
        tcp_ok: bool,
        dns_ok: bool,
        is_fallback: bool = False,
        tcp_ping_ms: int | None = None,
    ) -> None:
        """Update proxy ping status."""
        now = datetime.utcnow().isoformat()

        # Determine failed_since value: set when proxy becomes FAILED, clear when recovers
        cursor = await self.conn.execute(
            "SELECT ping_status, failed_since FROM proxies WHERE id = ?",
            (proxy_id,),
        )
        existing = await cursor.fetchone()
        existing_failed_since = existing["failed_since"] if existing else None
        existing_status = existing["ping_status"] if existing else None

        if ping_status.value == "failed":
            if existing_status != "failed":
                failed_since_val = now
            else:
                failed_since_val = existing_failed_since
        else:
            failed_since_val = None

        await self.conn.execute(
            """
            UPDATE proxies
            SET ping_ms = ?, ping_status = ?, tcp_ok = ?, dns_ok = ?, is_fallback = ?, tcp_ping_ms = ?, last_checked = ?, failed_since = ?
            WHERE id = ?
            """,
            (
                ping_ms,
                ping_status.value,
                int(tcp_ok),
                int(dns_ok),
                int(is_fallback),
                tcp_ping_ms,
                now,
                failed_since_val,
                proxy_id,
            ),
        )
        await self.conn.commit()

    async def add_proxy(self, proxy: ProxyBase) -> ProxyInDB | None:
        """Add a new proxy. Returns None if duplicate."""
        now = datetime.utcnow().isoformat()
        try:
            cursor = await self.conn.execute(
                """
                INSERT INTO proxies (server, port, secret, proxy_type, note, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    proxy.server,
                    proxy.port,
                    proxy.secret,
                    proxy.proxy_type,
                    proxy.note,
                    now,
                ),
            )
            await self.conn.commit()
            proxy_id = cursor.lastrowid
            cursor = await self.conn.execute(
                "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
            )
            row = await cursor.fetchone()
            return self._row_to_proxy(row) if row else None
        except aiosqlite.IntegrityError:
            return None

    async def get_proxy(self, proxy_id: int) -> ProxyInDB | None:
        """Get single proxy by ID."""
        cursor = await self.conn.execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_proxy(row) if row else None

    async def set_proxy_pinned(self, proxy_id: int, pinned: bool) -> None:
        """Set proxy pinned state."""
        await self.conn.execute(
            "UPDATE proxies SET pinned = ? WHERE id = ?",
            (int(pinned), proxy_id),
        )
        await self.conn.commit()

    async def update_proxy_note(self, proxy_id: int, note: str | None) -> None:
        """Update or clear a proxy note."""
        await self.conn.execute(
            "UPDATE proxies SET note = ? WHERE id = ?",
            (note, proxy_id),
        )
        await self.conn.commit()

    async def get_proxies(
        self,
        sort_by: SortBy = SortBy.LIKES,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ProxyInDB]:
        """Get list of proxies with sorting."""
        if limit is None:
            limit = config.app.page_size
        order_clause = {
            # Ensure pinned proxies appear first in Top (likes) view
            SortBy.LIKES: "pinned DESC, likes - dislikes DESC, likes DESC",
            SortBy.PING: """
                CASE
                    WHEN ping_status = 'ok' AND is_fallback = 0 THEN 0
                    WHEN ping_status = 'ok' AND is_fallback = 1 THEN 1
                    WHEN ping_status = 'warning' AND is_fallback = 0 THEN 2
                    WHEN ping_status = 'warning' AND is_fallback = 1 THEN 3
                    WHEN ping_status = 'failed' THEN 4
                    ELSE 5
                END ASC,
                CASE WHEN ping_ms IS NULL THEN 999999 ELSE ping_ms END ASC
            """,
            SortBy.NEWEST: "created_at DESC",
        }[sort_by]
        cursor = await self.conn.execute(
            f"SELECT * FROM proxies ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_proxy(row) for row in rows]

    async def get_total_count(self) -> int:
        """Get total proxy count."""
        cursor = await self.conn.execute("SELECT COUNT(*) FROM proxies")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def vote(
        self,
        proxy_id: int,
        voter_id: str,
        vote_type: str,
    ) -> tuple[int, int, str | None] | None:
        """
        Record, switch or remove a vote.

        Clicking the same vote again removes it. Returns
        ``(likes, dislikes, current_vote)`` where ``current_vote`` is the
        resulting vote ("like"/"dislike") or ``None`` when it was removed.
        """
        now = datetime.utcnow().isoformat()
        cursor = await self.conn.execute(
            "SELECT vote_type FROM votes WHERE proxy_id = ? AND voter_id = ?",
            (proxy_id, voter_id),
        )
        existing = await cursor.fetchone()
        if existing:
            old_type = existing["vote_type"]
            if old_type == vote_type:
                async with self.transaction():
                    await self.conn.execute(
                        "DELETE FROM votes WHERE proxy_id = ? AND voter_id = ?",
                        (proxy_id, voter_id),
                    )
                    column = "likes" if vote_type == "like" else "dislikes"
                    await self.conn.execute(
                        f"UPDATE proxies SET {column} = MAX({column} - 1, 0) WHERE id = ?",
                        (proxy_id,),
                    )
                current_vote = None
            else:
                async with self.transaction():
                    await self.conn.execute(
                        "UPDATE votes SET vote_type = ?, created_at = ? WHERE proxy_id = ? AND voter_id = ?",
                        (vote_type, now, proxy_id, voter_id),
                    )
                    if vote_type == "like":
                        await self.conn.execute(
                            "UPDATE proxies SET likes = likes + 1, dislikes = MAX(dislikes - 1, 0) WHERE id = ?",
                            (proxy_id,),
                        )
                    else:
                        await self.conn.execute(
                            "UPDATE proxies SET dislikes = dislikes + 1, likes = MAX(likes - 1, 0) WHERE id = ?",
                            (proxy_id,),
                        )
                current_vote = vote_type
        else:
            async with self.transaction():
                await self.conn.execute(
                    "INSERT INTO votes (proxy_id, voter_id, vote_type, created_at) VALUES (?, ?, ?, ?)",
                    (proxy_id, voter_id, vote_type, now),
                )
                column = "likes" if vote_type == "like" else "dislikes"
                await self.conn.execute(
                    f"UPDATE proxies SET {column} = {column} + 1 WHERE id = ?",
                    (proxy_id,),
                )
            current_vote = vote_type
        cursor = await self.conn.execute(
            "SELECT likes, dislikes FROM proxies WHERE id = ?", (proxy_id,)
        )
        row = await cursor.fetchone()
        return (row["likes"], row["dislikes"], current_vote) if row else None

    async def get_vote(self, proxy_id: int, voter_id: str) -> str | None:
        """Get user's vote for a proxy."""
        cursor = await self.conn.execute(
            "SELECT vote_type FROM votes WHERE proxy_id = ? AND voter_id = ?",
            (proxy_id, voter_id),
        )
        row = await cursor.fetchone()
        return row["vote_type"] if row else None

    async def delete_most_disliked(self, min_dislikes: int | None = None) -> int:
        """
        Delete proxies when dislikes exceed half of likes, but never below min_dislikes.
        Returns count of deleted proxies.
        """
        if min_dislikes is None:
            min_dislikes = config.workers.min_dislikes

        cursor = await self.conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM proxies
            WHERE pinned = 0
              AND dislikes >= ?
              AND dislikes * 2 > likes
            """,
            (min_dislikes,),
        )
        row = await cursor.fetchone()
        deleted_count = row["count"] if row else 0
        if deleted_count > 0:
            await self.conn.execute(
                """
                DELETE FROM proxies
                WHERE pinned = 0
                  AND dislikes >= ?
                  AND dislikes * 2 > likes
                """,
                (min_dislikes,),
            )
            await self.conn.commit()
            await self.conn.execute(
                "UPDATE stats SET last_cleanup = ?",
                (datetime.utcnow().isoformat(),),
            )
            await self.conn.commit()
        return deleted_count

    async def delete_old_failed_proxies(self, days: int | None = None) -> int:
        """
        Delete proxies that have been in FAILED status for more than specified days.
        Returns count of deleted proxies.
        """
        if days is None:
            days = config.workers.failed_days
        cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
        cursor = await self.conn.execute(
            """
            DELETE FROM proxies
            WHERE ping_status = 'failed'
              AND failed_since IS NOT NULL
              AND failed_since < ?
              AND pinned = 0
            """,
            (cutoff,),
        )
        deleted_count = cursor.rowcount
        if deleted_count > 0:
            await self.conn.execute(
                "UPDATE stats SET last_cleanup = ?",
                (datetime.utcnow().isoformat(),),
            )
        await self.conn.commit()
        return deleted_count

    async def get_stats(self) -> dict:
        """Get aggregate statistics."""
        cursor = await self.conn.execute("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(likes), 0) as total_likes,
                COALESCE(SUM(dislikes), 0) as total_dislikes,
                AVG(CASE WHEN ping_ms IS NOT NULL THEN ping_ms END) as avg_ping,
                SUM(CASE WHEN ping_status = 'ok' OR ping_status = 'warning' THEN 1 ELSE 0 END) as online
            FROM proxies
        """)
        row = await cursor.fetchone()
        cursor2 = await self.conn.execute("SELECT last_cleanup FROM stats WHERE id = 1")
        stats_row = await cursor2.fetchone()
        return {
            "total_proxies": row["total"] or 0,
            "total_likes": row["total_likes"] or 0,
            "total_dislikes": row["total_dislikes"] or 0,
            "avg_ping_ms": round(row["avg_ping"], 1) if row["avg_ping"] else None,
            "online_count": row["online"] or 0,
            "last_cleanup": datetime.fromisoformat(stats_row["last_cleanup"])
            if stats_row and stats_row["last_cleanup"]
            else None,
        }

    async def get_all_for_ping(
        self, skip_failed_hours: int | None = None
    ) -> list[tuple[int, str, int, str, ProxyType]]:
        """
        Get proxies for ping checking. Returns (id, server, port, secret).
        Skips proxies that failed recently (within skip_failed_hours) to avoid spamming dead proxies.
        """
        if skip_failed_hours is None:
            skip_failed_hours = config.workers.skip_failed_hours
        cutoff = (datetime.utcnow() - timedelta(hours=skip_failed_hours)).isoformat()
        cursor = await self.conn.execute(
            """
            SELECT id, server, port, secret, proxy_type FROM proxies
            WHERE ping_status != 'failed'
               OR last_checked IS NULL
               OR last_checked < ?
        """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [
            (
                row["id"],
                row["server"],
                row["port"],
                row["secret"],
                ProxyType(row["proxy_type"] or ProxyType.MT_PROTO),
            )
            for row in rows
        ]


db = Database()
