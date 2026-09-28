"""Async Postgres helpers for Return Prime tables."""

from __future__ import annotations

from typing import Any

from fashion_bot.database_manager import get_async_postgres_connection


class _PostgresAdapter:
    async def fetch_one(self, query: str, params: tuple[Any, ...] = ()) -> dict | None:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                row = await cur.fetchone()
                return dict(row) if row else None

    async def fetch_all(self, query: str, params: tuple[Any, ...] = ()) -> list[dict]:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                return [dict(row) for row in rows] if rows else []


class _Db:
    def __init__(self) -> None:
        self.postgres = _PostgresAdapter()

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        async with get_async_postgres_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(query, params)


db = _Db()
