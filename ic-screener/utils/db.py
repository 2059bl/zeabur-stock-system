"""
資料庫連線工具（共用 momentum-screener 的 PostgreSQL）
"""
import os
import asyncpg
from typing import Optional

DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://postgres:password@postgresql:5432/quant_db"
)
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            DATABASE_URL, min_size=2, max_size=10, command_timeout=60
        )
    return _pool


async def fetch_all(sql: str, *args) -> list[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
        return [dict(r) for r in rows]


async def fetch_one(sql: str, *args) -> Optional[dict]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
        return dict(row) if row else None


async def execute(sql: str, *args) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.execute(sql, *args)
