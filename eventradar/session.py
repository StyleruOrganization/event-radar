"""Interactive owner-only Telegram reader login; never used by the running bot."""

import asyncio
import getpass
import os
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession


async def main() -> None:
    os.umask(0o077)
    load_dotenv()
    api_id = int(os.environ.get("TELEGRAM_API_ID") or input("Telegram API ID: "))
    api_hash = os.environ.get("TELEGRAM_API_HASH") or getpass.getpass("Telegram API hash: ")
    destination = Path(os.environ.get("SESSION_OUTPUT", "data/reader-session.txt"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise RuntimeError("Session output already exists; choose a different SESSION_OUTPUT")
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(phone=lambda: input("Telegram phone (+country code): "), password=lambda: getpass.getpass("Telegram 2FA password: "))
        me = await client.get_me()
        if me.bot:
            raise RuntimeError("Use a user account, not a bot token")
        print(f"Telegram reader user ID: {me.id}")
        # Exclusive creation prevents accidental replacement and follows restrictive umask.
        with destination.open("x") as output:
            output.write(client.session.save())
        print(f"Reader session saved to {destination}. Copy its content to TELEGRAM_READER_SESSION; never commit it.")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
