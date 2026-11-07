"""Local liveness probe. Does not call Telegram, OpenAI or routing providers."""

import os
import sqlite3
import time


def main() -> None:
    path = os.environ.get("DATABASE_PATH", "/data/eventradar.sqlite3")
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE key='heartbeat'").fetchone()
        if not row or not 0 <= time.time() - float(row[0]) < 90:
            raise SystemExit(1)
    except (sqlite3.Error, ValueError):
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
