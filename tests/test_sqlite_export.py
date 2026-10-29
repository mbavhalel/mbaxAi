"""Tests for the optional legacy-data conversion; uses Python's stdlib only."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest


SPEC = importlib.util.spec_from_file_location("export_sqlite", Path(__file__).resolve().parents[1] / "scripts/export_sqlite.py")
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class ExportTests(unittest.TestCase):
    def test_export_preserves_committed_wal_and_quotes_names_without_changing_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "stock.db"
            db = sqlite3.connect(path)
            try:
                db.executescript("""
                    PRAGMA journal_mode=WAL;
                    PRAGMA wal_autocheckpoint=0;
                    CREATE TABLE products(uid,name,stock,low);
                    CREATE TABLE readings(ts,temp,hum);
                    CREATE TABLE scans(id,ts,kind,uid,name,stock_after);
                    CREATE TABLE ignored(uid);
                    CREATE TABLE settings(key,value);
                    INSERT INTO products VALUES('04:A1:B2:C3','Farmer''s \\ apples',2,3);
                    INSERT INTO readings VALUES(1000,22.5,48);
                    INSERT INTO scans VALUES(7,1000,'scan','04:A1:B2:C3','Apples',2);
                    INSERT INTO settings VALUES('tMin',12);
                """)
                db.commit()
                snapshots = {file: file.read_bytes() for file in Path(directory).iterdir()}
                sql = EXPORTER.export(path)
                self.assertIn("Farmer''s \\ apples", sql)
                self.assertIn("VALUES(1000,22.5,48)", sql)
                self.assertIn("t_min=12", sql)
                self.assertIn("destination storeroom already contains data", sql)
                self.assertIn("cancelled_at=now()", sql)
                for file, content in snapshots.items():
                    self.assertEqual(file.read_bytes(), content)
            finally:
                db.close()

    def test_invalid_numbers_and_nul_are_rejected(self):
        for value in (float("nan"), float("inf"), "bad\x00name"):
            with self.assertRaises(ValueError):
                EXPORTER.literal(value)


if __name__ == "__main__":
    unittest.main()
