#!/usr/bin/env python3
"""Export an old storeroom SQLite database for import in Supabase's SQL editor.

Run: python3 scripts/export_sqlite.py --source storeroom.db --output stock-import.sql
The destination Supabase storeroom must be empty. The source is never modified.
"""
import argparse
import math
from pathlib import Path
import sqlite3


def literal(value):
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        if not math.isfinite(value):
            raise ValueError("The old database contains a non-finite number")
        return str(value)
    if "\x00" in str(value):
        raise ValueError("The old database contains a NUL character")
    return "'" + str(value).replace("'", "''") + "'"


def export(source):
    statements = [
        "-- Private stock data: do not commit this file or put it on static hosting.",
        "BEGIN;", "SET standard_conforming_strings = on;",
        "SELECT pg_advisory_xact_lock(774411);",
        "DO $$ BEGIN IF EXISTS(SELECT 1 FROM public.products) "
        "OR EXISTS(SELECT 1 FROM public.scans) OR EXISTS(SELECT 1 FROM public.readings) "
        "OR EXISTS(SELECT 1 FROM public.ignored) THEN RAISE EXCEPTION "
        "'Import refused: destination storeroom already contains data'; END IF; END $$;",
    ]
    # Read from a temporary copy including WAL/SHM, so even SQLite's shared-memory
    # housekeeping cannot touch the original tracked snapshot.
    import shutil
    import tempfile
    with tempfile.TemporaryDirectory(prefix="storeroom-export-") as directory:
        copied = Path(directory) / "source.db"
        shutil.copyfile(source, copied)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists():
                shutil.copyfile(sidecar, Path(str(copied) + suffix))
        db = sqlite3.connect(copied.as_uri() + "?mode=ro", uri=True)
        try:
            def insert(table, columns, rows):
                for row in rows:
                    statements.append("INSERT INTO public.%s(%s) VALUES(%s);" % (
                        table, ",".join(columns), ",".join(literal(value) for value in row)))

            insert("products", ("uid", "name", "stock", "low"), db.execute(
                "SELECT uid,name,stock,low FROM products ORDER BY uid"))
            insert("readings", ("ts", "temp", "hum"), db.execute(
                "SELECT ts,temp,hum FROM readings ORDER BY ts"))
            insert("scans", ("id", "ts", "kind", "uid", "name", "stock_after"), db.execute(
                "SELECT id,ts,kind,uid,name,stock_after FROM scans ORDER BY id"))
            insert("ignored", ("uid",), db.execute("SELECT uid FROM ignored ORDER BY uid"))
            settings = {"tMin": 15, "tMax": 25, "hMin": 30, "hMax": 60}
            settings.update({key: value for key, value in db.execute(
                "SELECT key,value FROM settings") if key in settings})
            statements.append("UPDATE public.settings SET %s WHERE singleton;" % ",".join(
                column + "=" + literal(settings[key]) for key, column in (
                    ("tMin", "t_min"), ("tMax", "t_max"), ("hMin", "h_min"), ("hMax", "h_max"))))
        finally:
            db.close()
    statements.extend([
        "SELECT setval('public.scans_id_seq', greatest(coalesce((SELECT max(id) FROM public.scans),0),1), "
        "EXISTS(SELECT 1 FROM public.scans));",
        "-- Import establishes the current alert baseline; do not notify on old stock.",
        "UPDATE public.alerts SET cancelled_at=now() WHERE delivered_at IS NULL AND cancelled_at IS NULL;",
        "COMMIT;", "",
    ])
    return "\n".join(statements)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error("Source database does not exist")
    sql = export(source)
    # Exclusive creation prevents overwriting an export or the source database.
    with args.output.expanduser().open("x", encoding="utf-8") as output:
        output.write(sql)
    print("Exported stock to", args.output)


if __name__ == "__main__":
    main()
