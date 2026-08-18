"""PostgreSQL 連線 — 共用現有 Zeabur PostgreSQL。"""
import os
import asyncpg
import logging

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        dsn = os.environ["DATABASE_URL"]
        _pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
        logger.info("PostgreSQL 連線池建立成功")
    return _pool


async def execute(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)


async def fetch_all(sql: str, *args) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]


async def fetch_one(sql: str, *args) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def fetch_val(sql: str, *args):
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetchval(sql, *args)


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
