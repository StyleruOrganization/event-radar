"""Deployment preflight and SQLite backup for the shared infrastructure runner."""

import argparse
import sqlite3
from datetime import UTC, datetime

from pydantic import ValidationError

from eventradar.chatgpt_auth import AuthRequired, ChatGPTCredentials
from eventradar.config import Settings
from eventradar.runtime import instance_lock
from eventradar.storage import backup


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("preflight", "backup"))
    args = parser.parse_args()
    try:
        settings = Settings()
        if args.action == "preflight":
            profile = ChatGPTCredentials(settings.chatgpt_auth_dir).profile()
            if not all(profile.get(key) for key in ("client_id", "subject", "access_token", "refresh_token")):
                raise RuntimeError("Import a fresh ChatGPT session before the first release")
            if "chatgpt.tokens.use.direct" not in profile.get("scopes", []):
                raise RuntimeError("ChatGPT session does not grant plan usage")
            print("Deployment preflight completed")
            return
        with instance_lock(settings.database_path):
            if not settings.database_path.exists():
                print("First release: the application will create a new SQLite database")
                return
            directory = settings.database_path.parent / "backups"
            destination = directory / (datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f") + ".sqlite3")
            backup(settings.database_path, destination)
            for stale in sorted(directory.glob("*.sqlite3"))[:-10]:
                stale.unlink()
        print(f"SQLite backup created: {destination.name}")
    except ValidationError as error:
        fields = ", ".join(str(entry["loc"][0]) for entry in error.errors() if entry["loc"])
        raise SystemExit(f"Invalid server environment fields: {fields}") from None
    except (AuthRequired, OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        # Credential values and connection details must stay out of Actions logs.
        raise SystemExit(f"Deployment {args.action} failed ({type(error).__name__}). Check server settings and mounted data.") from None


if __name__ == "__main__":
    main()
