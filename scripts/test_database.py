#!/usr/bin/env python3
"""Run migration, RLS and concurrency checks in a disposable local PostgreSQL.

Requires Docker. Never connects to or changes a Supabase project.
"""
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    environment["POSTGRES_PASSWORD"] = secrets.token_urlsafe(24)
    container = "mbaxai-db-test-" + secrets.token_hex(5)
    docker = ["docker", "--host=unix:///var/run/docker.sock"]

    def run(*args, input=None, check=True):
        result = subprocess.run(docker + list(args), input=input, text=True, env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result

    def sql(source):
        return run("exec", "-i", container, "psql", "-X", "-q", "-t", "-A", "-v",
                   "ON_ERROR_STOP=1", "-U", "postgres", input=source).stdout.strip()

    try:
        run("run", "--detach", "--name", container, "--env", "POSTGRES_PASSWORD",
            "postgres:17-alpine", "-c", "wal_level=logical")
        for _ in range(100):
            if run("exec", container, "pg_isready", "-U", "postgres", check=False).returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Test PostgreSQL did not start")
        sql("""
            CREATE ROLE anon NOLOGIN;
            CREATE ROLE authenticated NOLOGIN;
            CREATE ROLE service_role NOLOGIN BYPASSRLS;
            CREATE SCHEMA auth;
            CREATE TABLE auth.users(id uuid PRIMARY KEY);
            CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
            $$ SELECT nullif(current_setting('request.jwt.claim.sub',true),'')::uuid $$;
            GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
            CREATE PUBLICATION supabase_realtime;
        """)
        for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
            sql(migration.read_text())
        print("Supabase migration applied to PostgreSQL 17.", flush=True)
        sql((ROOT / "supabase/tests/storeroom.sql").read_text())
        print("Database role, membership, stock, deduplication and alert tests passed.", flush=True)

        user_id = "00000000-0000-0000-0000-000000000099"
        sql(f"""
            INSERT INTO auth.users(id) VALUES('{user_id}');
            INSERT INTO public.members(user_id) VALUES('{user_id}');
            SET ROLE authenticated;
            SET request.jwt.claim.sub='{user_id}';
            SELECT public.manage_stock('add','{{"uid":"04:A1:B2:C3","name":"Race test","stock":20,"low":3}}');
        """)

        def scan(event_id):
            payload = json.dumps({"type": "scan", "uid": "04:A1:B2:C3", "event_id": event_id})
            return json.loads(sql("SET ROLE service_role; SELECT public.ingest_device('" + payload + "'::jsonb);"))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(scan, [str(uuid.uuid4()) for _ in range(12)]))
        assert all(result["duplicate"] is False for result in results)
        repeated_id = str(uuid.uuid4())
        with ThreadPoolExecutor(max_workers=8) as pool:
            duplicates = list(pool.map(scan, [repeated_id] * 8))
        assert sum(not result["duplicate"] for result in duplicates) == 1
        assert sql("SELECT stock FROM public.products WHERE uid='04:A1:B2:C3';") == "7"
        assert sql("SELECT count(*) FROM public.scans;") == "13"
        print("Concurrent scans decrement once each; simultaneous retries decrement once total.", flush=True)

        # Verify the optional migration utility's actual SQL against the same schema.
        sql("TRUNCATE public.products,public.readings,public.scans,public.ignored,public.alerts,public.device_events RESTART IDENTITY;")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.db"
            connection = sqlite3.connect(path)
            connection.executescript("""
                CREATE TABLE products(uid,name,stock,low);
                CREATE TABLE readings(ts,temp,hum);
                CREATE TABLE scans(id,ts,kind,uid,name,stock_after);
                CREATE TABLE ignored(uid);
                CREATE TABLE settings(key,value);
                INSERT INTO products VALUES('04:A1:B2:C3','Farmer''s apples',2,3);
                INSERT INTO readings VALUES(1000,22.5,48);
                INSERT INTO scans VALUES(7,1000,'scan','04:A1:B2:C3','Apples',2);
                INSERT INTO settings VALUES('tMin',12);
            """)
            connection.close()
            spec = importlib.util.spec_from_file_location("export_sqlite", ROOT / "scripts/export_sqlite.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            exported = module.export(path)
            sql(exported)
            assert sql("SELECT stock FROM public.products;") == "2"
            assert sql("SELECT name FROM public.products;") == "Farmer's apples"
            assert sql("SELECT t_min FROM public.settings;") == "12"
            assert sql("SELECT count(*) FROM public.alerts WHERE cancelled_at IS NULL;") == "0"
            try:
                sql(exported)
            except RuntimeError as error:
                assert "destination storeroom already contains data" in str(error)
            else:
                raise AssertionError("Import overwrote a nonempty target")
        print("SQLite import preserves stock/settings/history and refuses a nonempty target.", flush=True)
    finally:
        run("rm", "--force", "--volumes", container, check=False)


if __name__ == "__main__":
    main()
