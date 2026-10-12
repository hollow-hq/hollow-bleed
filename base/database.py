import asyncio
import inspect
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from asyncmy.cursors import DictCursor


class Database:
    """MySQL adapter exposing an asyncpg-like API (fetch, fetchrow, fetchval, execute).

    Results are dicts (dict-style column access: row['guild_id']), so existing
    asyncpg-style code works unchanged.

    Every statement acquires a pooled connection, opens a cursor on it, closes
    that cursor and hands the connection back. Closing the cursor matters: an
    unreleased cursor keeps the connection marked dirty, so asyncmy discards it
    on release (a reconnect round trip) or the pool starves and `acquire()`
    blocks — which is what made commands take tens of seconds.
    """

    def __init__(self, pool):
        self.pool = pool

    @asynccontextmanager
    async def _cursor(self) -> AsyncIterator[tuple]:
        """Yield (connection, cursor) and always clean both up."""
        conn = await self.pool.acquire()
        try:
            cur = conn.cursor(DictCursor)
            try:
                yield conn, cur
            finally:
                try:
                    await cur.close()
                except Exception:
                    # A broken cursor must not mask the query error above.
                    pass
        finally:
            result = self.pool.release(conn)
            if inspect.isawaitable(result):
                await result

    async def fetch(self, query: str, *args: Any) -> List[Dict[str, Any]]:
        async with self._cursor() as (_, cur):
            await cur.execute(query, args if args else None)
            return await cur.fetchall()

    async def fetchrow(self, query: str, *args: Any) -> Optional[Dict[str, Any]]:
        async with self._cursor() as (_, cur):
            await cur.execute(query, args if args else None)
            return await cur.fetchone()

    async def fetchval(self, query: str, *args: Any) -> Any:
        async with self._cursor() as (_, cur):
            await cur.execute(query, args if args else None)
            row = await cur.fetchone()
            if row:
                return next(iter(row.values()))
            return None

    async def execute(self, query: str, *args: Any) -> None:
        async with self._cursor() as (_, cur):
            await cur.execute(query, args if args else None)

    async def fetchmany(
        self, query: str, size: int, *args: Any
    ) -> List[Dict[str, Any]]:
        """Like fetch, but pulls at most `size` rows (cursor still fully closed)."""
        async with self._cursor() as (_, cur):
            await cur.execute(query, args if args else None)
            return await cur.fetchmany(size)

    async def executemany(self, query: str, args: List[Tuple[Any, ...]]) -> None:
        if not args:
            return
        async with self._cursor() as (_, cur):
            await cur.executemany(query, args)

    async def close(self) -> None:
        self.pool.close()
        await self.pool.wait_closed()
