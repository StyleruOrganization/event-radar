"""One writer application per database; shared by the bot and restore command."""

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def instance_lock(database: Path) -> Iterator[None]:
    database.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(database.with_suffix(database.suffix + ".lock"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("С этой базой уже работает EventRadar. Сначала остановите его.") from None
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
