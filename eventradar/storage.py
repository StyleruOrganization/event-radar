"""Consistent SQLite migration without secrets or overwriting a server database."""

import argparse
import os
import sqlite3
import shutil
import sys
import tempfile
from pathlib import Path

from eventradar.runtime import instance_lock


def backup(source: Path, destination: Path) -> None:
    source, destination = source.expanduser().absolute(), destination.expanduser().absolute()
    if not source.is_file():
        raise ValueError("Исходная база не существует.")
    if destination.exists():
        raise ValueError("Файл назначения уже существует. Выберите новое имя; существующие данные не перезаписываются.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".eventradar-backup-", dir=destination.parent)
    os.close(descriptor)
    try:
        uri = source.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=30) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if version not in {1, 2, 3, 4, 5} or not {"users", "channels", "subscriptions", "posts", "deliveries"} <= tables:
                raise ValueError("Это не поддерживаемая база EventRadar.")
            with sqlite3.connect(temporary) as output:
                connection.backup(output)
                if output.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise ValueError("Проверка целостности копии не прошла; перенос отменен.")
        with open(temporary, "rb") as output:
            os.fsync(output.fileno())
        # Atomic no-replace installation: refuse even a concurrently created destination.
        os.link(temporary, destination)
    finally:
        os.unlink(temporary)


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="EventRadar SQLite backup and initial server restore")
    parser.add_argument("--database", type=Path, help="Database path; otherwise DATABASE_PATH from .env")
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("backup")
    export.add_argument("--output", type=Path, required=True)
    restore = commands.add_parser("restore")
    source = restore.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path)
    source.add_argument("--stdin", action="store_true", help="Receive SQLite backup over SSH stdin")
    arguments = parser.parse_args()
    try:
        if arguments.database is not None:
            database = arguments.database
        else:
            from eventradar.config import Settings
            database = Settings().database_path
        if arguments.command == "backup":
            backup(database, arguments.output)
            print("Согласованная копия базы сохранена:", arguments.output)
        else:
            with instance_lock(database):
                if arguments.stdin:
                    with tempfile.TemporaryDirectory(prefix="eventradar-import-") as temporary:
                        incoming = Path(temporary) / "incoming.sqlite3"
                        with incoming.open("wb") as output:
                            shutil.copyfileobj(sys.stdin.buffer, output)
                        backup(incoming, database)
                else:
                    backup(arguments.input, database)
            print("База перенесена. Подписки, настройки, история и граница последнего отчета сохранены.")
    except (OSError, sqlite3.Error, ValueError, RuntimeError) as error:
        print(str(error))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
