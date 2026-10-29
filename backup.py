"""Create a consistent SQLite backup, including committed WAL data.

Usage: python backup.py /path/to/backup.db [--source /path/to/storeroom.db]
"""
import argparse
import os
from pathlib import Path
import sqlite3


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--source", type=Path, default=Path(os.getenv(
        "DB_PATH", str(Path.home() / ".mbaxai" / "storeroom.db"))))
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    destination = args.destination.expanduser().resolve()
    if not source.is_file():
        parser.error("Source database does not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation prevents overwriting an existing backup or the source.
    with destination.open("xb"):
        pass
    try:
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
    except Exception:
        destination.unlink()
        raise
    print("Backup saved to", destination)


if __name__ == "__main__":
    main()
