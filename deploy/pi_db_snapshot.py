"""Write a consistent snapshot of the HomeSec database to stdout.

Runs on the Pi through ``ssh ... python3 - <backend_dir>``, so it must stay
standard-library only. VACUUM INTO reads a live WAL database safely and gives
a transaction-consistent copy (https://www.sqlite.org/lang_vacuum.html).
"""

import pathlib
import shutil
import sqlite3
import sys
import tempfile

DEFAULT_DATABASE = "./homesec.db"
# Later files win: systemd exports .env, which outranks conf.d in pydantic-settings.
ENV_FILES = ("conf.d/homesec.env", ".env")


def database_path(backend: pathlib.Path) -> pathlib.Path:
    value = DEFAULT_DATABASE
    for name in ENV_FILES:
        env_file = backend / name
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, raw = line.strip().partition("=")
            if separator and key.strip() == "HS_DATABASE_PATH":
                value = raw.strip().strip("'\"")
    path = pathlib.Path(value)
    return path if path.is_absolute() else backend / path


def main() -> None:
    source = database_path(pathlib.Path(sys.argv[1]))
    with tempfile.TemporaryDirectory(prefix="homesec-backup-", dir="/var/tmp") as work:
        snapshot = pathlib.Path(work) / "snapshot.db"
        connection = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=60)
        try:
            connection.execute("VACUUM INTO ?", (str(snapshot),))
        finally:
            connection.close()
        with snapshot.open("rb") as stream:
            shutil.copyfileobj(stream, sys.stdout.buffer)


if __name__ == "__main__":
    main()
