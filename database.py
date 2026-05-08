"""
Database layer for MTProto Proxy Hub.
Async SQLite with aiosqlite.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

import aiosqlite

from models import PingStatus, ProxyBase, ProxyInDB, SortBy

DATABASE_PATH = Path("data/proxies.db")


class Database:
    """Async SQLite database manager."""

    def __init__(self, db_path: Path = DATABASE_PATH) -> None:
        self.db_path = db_path
        self._connection: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Initialize database connection and create tables."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = await aiosqlite.connect(self.db_path)
        self._connection.row_factory = aiosqlite.Row
        await self._create_tables()

    async def close(self) -> None:
        """Close database connection."""
        if self._connection:
            await self._connection.close()
            self._connection = None

    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[aiosqlite.Connection]:
        """Context manager for transactions."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)
        try:
            yield self._connection
            await self._connection.commit()
        except Exception:
            await self._connection.rollback()
            raise

    async def _create_tables(self) -> None:
        """Create database tables if not exist."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS proxies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                server TEXT NOT NULL,
                port INTEGER NOT NULL,
                secret TEXT NOT NULL,
                likes INTEGER DEFAULT 0,
                dislikes INTEGER DEFAULT 0,
                ping_ms INTEGER,
                ping_status TEXT DEFAULT 'pending',
                tcp_ok INTEGER DEFAULT 0,
                dns_ok INTEGER DEFAULT 0,
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
        await self._connection.commit()

    def _row_to_proxy(self, row: aiosqlite.Row) -> ProxyInDB:
        """Convert database row to ProxyInDB model."""
        return ProxyInDB(
            id=row["id"],
            server=row["server"],
            port=row["port"],
            secret=row["secret"],
            likes=row["likes"],
            dislikes=row["dislikes"],
            ping_ms=row["ping_ms"],
            ping_status=PingStatus(row["ping_status"]),
            tcp_ok=bool(row["tcp_ok"]),
            dns_ok=bool(row["dns_ok"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            last_checked=datetime.fromisoformat(row["last_checked"])
            if row["last_checked"]
            else None,
        )

    async def add_proxy(self, proxy: ProxyBase) -> ProxyInDB | None:
        """Add a new proxy. Returns None if duplicate."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        now = datetime.utcnow().isoformat()
        try:
            cursor = await self._connection.execute(
                """
                INSERT INTO proxies (server, port, secret, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (proxy.server, proxy.port, proxy.secret, now),
            )
            await self._connection.commit()
            proxy_id = cursor.lastrowid

            cursor = await self._connection.execute(
                "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
            )
            row = await cursor.fetchone()
            return self._row_to_proxy(row) if row else None
        except aiosqlite.IntegrityError:
            return None

    async def get_proxy(self, proxy_id: int) -> ProxyInDB | None:
        """Get single proxy by ID."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cursor = await self._connection.execute(
            "SELECT * FROM proxies WHERE id = ?", (proxy_id,)
        )
        row = await cursor.fetchone()
        return self._row_to_proxy(row) if row else None

    async def get_proxies(
        self,
        sort_by: SortBy = SortBy.LIKES,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ProxyInDB]:
        """Get list of proxies with sorting."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        order_clause = {
            SortBy.LIKES: "likes - dislikes DESC, likes DESC",
            SortBy.PING: """
                CASE
                    WHEN ping_status = 'ok' THEN 0
                    WHEN ping_status = 'warning' THEN 1
                    WHEN ping_status = 'failed' THEN 2
                    ELSE 3
                END ASC,
                CASE WHEN ping_ms IS NULL THEN 999999 ELSE ping_ms END ASC
            """,
            SortBy.NEWEST: "created_at DESC",
        }[sort_by]

        cursor = await self._connection.execute(
            f"SELECT * FROM proxies ORDER BY {order_clause} LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()
        return [self._row_to_proxy(row) for row in rows]

    async def get_total_count(self) -> int:
        """Get total proxy count."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cursor = await self._connection.execute("SELECT COUNT(*) FROM proxies")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def vote(
        self,
        proxy_id: int,
        voter_id: str,
        vote_type: str,
    ) -> tuple[int, int] | None:
        """
        Record a vote. Returns (likes, dislikes) or None if already voted.
        """
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        now = datetime.utcnow().isoformat()

        cursor = await self._connection.execute(
            "SELECT vote_type FROM votes WHERE proxy_id = ? AND voter_id = ?",
            (proxy_id, voter_id),
        )
        existing = await cursor.fetchone()

        if existing:
            old_type = existing["vote_type"]
            if old_type == vote_type:
                return None

            async with self.transaction():
                await self._connection.execute(
                    "UPDATE votes SET vote_type = ?, created_at = ? WHERE proxy_id = ? AND voter_id = ?",
                    (vote_type, now, proxy_id, voter_id),
                )

                if vote_type == "like":
                    await self._connection.execute(
                        "UPDATE proxies SET likes = likes + 1, dislikes = dislikes - 1 WHERE id = ?",
                        (proxy_id,),
                    )
                else:
                    await self._connection.execute(
                        "UPDATE proxies SET dislikes = dislikes + 1, likes = likes - 1 WHERE id = ?",
                        (proxy_id,),
                    )
        else:
            async with self.transaction():
                await self._connection.execute(
                    "INSERT INTO votes (proxy_id, voter_id, vote_type, created_at) VALUES (?, ?, ?, ?)",
                    (proxy_id, voter_id, vote_type, now),
                )
                column = "likes" if vote_type == "like" else "dislikes"
                await self._connection.execute(
                    f"UPDATE proxies SET {column} = {column} + 1 WHERE id = ?",
                    (proxy_id,),
                )

        cursor = await self._connection.execute(
            "SELECT likes, dislikes FROM proxies WHERE id = ?", (proxy_id,)
        )
        row = await cursor.fetchone()
        return (row["likes"], row["dislikes"]) if row else None

    async def get_vote(self, proxy_id: int, voter_id: str) -> str | None:
        """Get user's vote for a proxy."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cursor = await self._connection.execute(
            "SELECT vote_type FROM votes WHERE proxy_id = ? AND voter_id = ?",
            (proxy_id, voter_id),
        )
        row = await cursor.fetchone()
        return row["vote_type"] if row else None

    async def update_ping(
        self,
        proxy_id: int,
        ping_ms: int | None,
        ping_status: PingStatus,
        tcp_ok: bool,
        dns_ok: bool,
    ) -> None:
        """Update proxy ping status."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        now = datetime.utcnow().isoformat()
        await self._connection.execute(
            """
            UPDATE proxies
            SET ping_ms = ?, ping_status = ?, tcp_ok = ?, dns_ok = ?, last_checked = ?
            WHERE id = ?
            """,
            (ping_ms, ping_status.value, int(tcp_ok), int(dns_ok), now, proxy_id),
        )
        await self._connection.commit()

    async def delete_most_disliked(self, min_dislikes: int = 5) -> int | None:
        """
        Delete the proxy with most dislikes (if >= min_dislikes).
        Returns deleted proxy ID or None.
        """
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cursor = await self._connection.execute(
            """
            SELECT id FROM proxies
            WHERE dislikes >= ?
            ORDER BY dislikes DESC
            LIMIT 1
            """,
            (min_dislikes,),
        )
        row = await cursor.fetchone()

        if row:
            proxy_id = row["id"]
            await self._connection.execute(
                "DELETE FROM proxies WHERE id = ?", (proxy_id,)
            )
            await self._connection.commit()

            now = datetime.utcnow().isoformat()
            await self._connection.execute("UPDATE stats SET last_cleanup = ?", (now,))
            await self._connection.commit()

            return proxy_id
        return None

    async def get_stats(self) -> dict:
        """Get aggregate statistics."""
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cursor = await self._connection.execute("""
            SELECT
                COUNT(*) as total,
                COALESCE(SUM(likes), 0) as total_likes,
                COALESCE(SUM(dislikes), 0) as total_dislikes,
                AVG(CASE WHEN ping_ms IS NOT NULL THEN ping_ms END) as avg_ping,
                SUM(CASE WHEN ping_status = 'ok' OR ping_status = 'warning' THEN 1 ELSE 0 END) as online
            FROM proxies
        """)
        row = await cursor.fetchone()

        cursor2 = await self._connection.execute(
            "SELECT last_cleanup FROM stats WHERE id = 1"
        )
        stats_row = await cursor2.fetchone()

        return {
            "total_proxies": row["total"],
            "total_likes": row["total_likes"],
            "total_dislikes": row["total_dislikes"],
            "avg_ping_ms": round(row["avg_ping"], 1) if row["avg_ping"] else None,
            "online_count": row["online"],
            "last_cleanup": datetime.fromisoformat(stats_row["last_cleanup"])
            if stats_row and stats_row["last_cleanup"]
            else None,
        }

    async def get_all_for_ping(
        self, skip_failed_hours: int = 2
    ) -> list[tuple[int, str, int]]:
        """
        Get proxies for ping checking. Returns (id, server, port).
        Skips proxies that failed recently (within skip_failed_hours) to avoid spamming dead proxies.
        """
        if not self._connection:
            msg = "Database not connected"
            raise RuntimeError(msg)

        cutoff = datetime.utcnow()
        from datetime import timedelta

        cutoff = (cutoff - timedelta(hours=skip_failed_hours)).isoformat()

        cursor = await self._connection.execute(
            """
            SELECT id, server, port FROM proxies
            WHERE ping_status != 'failed'
               OR last_checked IS NULL
               OR last_checked < ?
        """,
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [(row["id"], row["server"], row["port"]) for row in rows]


db = Database()
