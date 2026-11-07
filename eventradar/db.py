import json
from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
from typing import AsyncIterator, Any

import aiosqlite


def message_chunks(text: str, limit: int = 3800) -> list[str]:
    """Telegram counts UTF-16 code units. Prefer paragraph boundaries."""
    chunks: list[str] = []
    remaining = text
    while remaining:
        size, end = 0, 0
        for index, character in enumerate(remaining):
            size += 2 if ord(character) > 0xFFFF else 1
            if size > limit:
                break
            end = index + 1
        if end < len(remaining):
            boundary = remaining.rfind("\n", 0, end)
            if boundary > end // 2:
                end = boundary + 1
        chunks.append(remaining[:end].rstrip())
        remaining = remaining[end:]
    return chunks or [""]


class Database:
    """Short independent transactions; never hold a SQLite lock across network I/O."""

    def __init__(self, path: Path):
        self.path = path

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        async with aiosqlite.connect(self.path, timeout=30) as connection:
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(self.path) as connection:
            version = (await (await connection.execute("PRAGMA user_version")).fetchone())[0]
            if version > 2:
                raise RuntimeError("Database is newer than this application; refuse unsafe downgrade")
            if version == 1:
                # Google consent does not authorize sending an address to a different provider.
                await connection.execute("UPDATE users SET routing=0")
            await connection.executescript(files("eventradar").joinpath("schema.sql").read_text())

    async def rows(self, sql: str, parameters: tuple = ()) -> list[dict[str, Any]]:
        async with aiosqlite.connect(self.path, timeout=30) as connection:
            connection.row_factory = aiosqlite.Row
            cursor = await connection.execute(sql, parameters)
            return [dict(row) for row in await cursor.fetchall()]

    async def one(self, sql: str, parameters: tuple = ()) -> dict[str, Any] | None:
        rows = await self.rows(sql, parameters)
        return rows[0] if rows else None

    async def execute(self, sql: str, parameters: tuple = ()) -> int:
        async with self.transaction() as connection:
            cursor = await connection.execute(sql, parameters)
            return cursor.lastrowid or 0

    async def enqueue(self, recipient: int, text: str, dedupe: str, keyboard: dict | None = None, *, sender: int | None = None) -> None:
        chunks = message_chunks(text)
        async with self.transaction() as connection:
            for index, chunk in enumerate(chunks):
                markup = json.dumps(keyboard) if keyboard and index == len(chunks) - 1 else None
                await connection.execute(
                    "INSERT OR IGNORE INTO outbox(recipient,sender,text,keyboard,dedupe) VALUES(?,?,?,?,?)",
                    (recipient, sender, chunk, markup, f"{dedupe}:{index}"),
                )
