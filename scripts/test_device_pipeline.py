#!/usr/bin/env python3
"""Test ESP32-format HTTP -> actual Edge Function -> PostgREST -> PostgreSQL.

Uses disposable local containers and synthetic credentials. No cloud project or
physical ESP32 is needed. Requires Docker, Node/npm, and Python 3 on Linux.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def jwt(secret, role, subject=None):
    def encoded(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    claims = {"role": role, "exp": int(time.time()) + 3600}
    if subject:
        claims["sub"] = subject
    body = encoded({"alg": "HS256", "typ": "JWT"}) + b"." + encoded(claims)
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).rstrip(b"=")
    return (body + b"." + signature).decode()


def main():
    environment = os.environ.copy()
    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_TLS", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        environment.pop(name, None)
    password, secret, device_token = (secrets.token_urlsafe(32) for _ in range(3))
    environment.update(POSTGRES_PASSWORD=password)
    prefix = "mbaxai-pipeline-" + secrets.token_hex(4)
    database, rest, network = prefix + "-db", prefix + "-rest", prefix + "-network"
    docker = ["docker", "--host=unix:///var/run/docker.sock"]
    edge_process = gateway = None

    def command(*args, input=None, check=True):
        result = subprocess.run(docker + list(args), input=input, text=True, env=environment,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and result.returncode:
            raise RuntimeError(result.stderr or result.stdout)
        return result.stdout.strip()

    def sql(statement):
        return command("exec", "-i", database, "psql", "-X", "-q", "-t", "-A", "-v",
                       "ON_ERROR_STOP=1", "-U", "postgres", input=statement)

    def request(port, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        try:
            connection.request("POST" if body is not None else "GET", path,
                               json.dumps(body) if body is not None else None,
                               {"Content-Type": "application/json", **(headers or {})})
            response = connection.getresponse()
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
        finally:
            connection.close()

    try:
        command("network", "create", network)
        command("run", "--detach", "--name", database, "--network", network,
                "--network-alias", "db", "--env", "POSTGRES_PASSWORD", "postgres:17-alpine", "-c", "wal_level=logical")
        for _ in range(100):
            ready = subprocess.run(docker + ["exec", database, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                                   env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if ready.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Test database did not start")
        sql("""
          CREATE ROLE anon NOLOGIN;
          CREATE ROLE authenticated NOLOGIN;
          CREATE ROLE service_role NOLOGIN BYPASSRLS;
          CREATE SCHEMA auth;
          CREATE TABLE auth.users(id uuid PRIMARY KEY);
          CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS
          $$ SELECT (nullif(current_setting('request.jwt.claims',true),'')::jsonb->>'sub')::uuid $$;
          GRANT USAGE ON SCHEMA public,auth TO anon,authenticated,service_role;
          CREATE PUBLICATION supabase_realtime;
        """)
        sql("CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD '" + password + "'; GRANT anon,authenticated,service_role TO authenticator;")
        for migration in sorted((ROOT / "supabase/migrations").glob("*.sql")):
            sql(migration.read_text())
        member_id = "00000000-0000-0000-0000-000000000099"
        sql(f"INSERT INTO auth.users(id) VALUES('{member_id}'); INSERT INTO public.members VALUES('{member_id}');")
        environment.update(PGRST_DB_URI=f"postgres://authenticator:{password}@db:5432/postgres",
                           PGRST_DB_SCHEMAS="public", PGRST_DB_ANON_ROLE="anon", PGRST_JWT_SECRET=secret)
        command("run", "--detach", "--name", rest, "--network", network, "--publish", "127.0.0.1::3000",
                "--env", "PGRST_DB_URI", "--env", "PGRST_DB_SCHEMAS", "--env", "PGRST_DB_ANON_ROLE",
                "--env", "PGRST_JWT_SECRET", "postgrest/postgrest:v14.18")
        rest_port = int(command("port", rest, "3000").rsplit(":", 1)[1])
        for _ in range(100):
            try:
                if request(rest_port, "/")[0] == 200:
                    break
            except (OSError, http.client.HTTPException, ValueError):
                pass
            time.sleep(0.2)
        else:
            raise RuntimeError("PostgREST did not start")

        # Supabase's gateway supplies /rest/v1. This local shim only strips that
        # prefix; PostgREST itself verifies JWTs and runs the actual SQL/RLS.
        class Gateway(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                if not self.path.startswith("/rest/v1/"):
                    self.send_error(404)
                    return
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                target = http.client.HTTPConnection("127.0.0.1", rest_port, timeout=10)
                try:
                    target.request("POST", self.path[len("/rest/v1"):], body,
                                   {key: value for key, value in self.headers.items() if key.lower() not in ("host", "connection", "transfer-encoding")})
                    upstream = target.getresponse()
                    data = upstream.read()
                    self.send_response(upstream.status)
                    self.send_header("Content-Type", upstream.getheader("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    target.close()

        gateway = ThreadingHTTPServer(("127.0.0.1", 0), Gateway)
        threading.Thread(target=gateway.serve_forever, daemon=True).start()
        gateway_port = gateway.server_address[1]
        edge_port = free_port()
        environment.update(SUPABASE_URL=f"http://127.0.0.1:{gateway_port}",
                           SUPABASE_SERVICE_ROLE_KEY=jwt(secret, "service_role"), DEVICE_TOKEN=device_token,
                           TG_TOKEN="", TG_CHAT_ID="", TEST_EDGE_PORT=str(edge_port),
                           DENO_DIR=str(ROOT.parent / ".cache/deno"), DENO_TLS_CA_STORE="system")
        with tempfile.TemporaryDirectory(prefix="mbaxai-device-test-") as directory:
            wrapper = Path(directory) / "serve.js"
            wrapper.write_text("const serve = Deno.serve.bind(Deno);\n"
                               "Deno.serve = handler => serve({hostname:'127.0.0.1',port:Number(Deno.env.get('TEST_EDGE_PORT'))},handler);\n"
                               "await import(" + json.dumps((ROOT / "supabase/functions/ingest-device/index.ts").as_uri()) + ");\n")
            log_path = Path(directory) / "edge.log"
            with log_path.open("w") as log:
                edge_process = subprocess.Popen(["npm", "exec", "--yes", "--package=deno@2.1.14", "--", "deno", "run",
                    "--frozen", "--allow-env", "--allow-net=127.0.0.1", "--config", str(ROOT / "supabase/functions/deno.json"), str(wrapper)],
                    cwd=ROOT, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            for _ in range(200):
                if edge_process.poll() is not None:
                    raise RuntimeError("Edge Function exited: " + log_path.read_text())
                try:
                    if request(edge_port, "/", {})[0] == 401:
                        break
                except (OSError, http.client.HTTPException):
                    pass
                time.sleep(0.2)
            else:
                raise RuntimeError("Edge Function did not start")

            device_headers = {"X-Device-Token": device_token}
            member_headers = {"Authorization": "Bearer " + jwt(secret, "authenticated", member_id)}
            def rpc(name, data):
                status, result = request(rest_port, "/rpc/" + name, data, member_headers)
                assert status == 200, (status, result)
                return result

            assert request(edge_port, "/", {"type": "climate", "t": 22.5, "h": 50}, {"X-Device-Token": "wrong"})[0] == 401
            assert request(edge_port, "/", {"type": "scan", "uid": "127C1207"}, device_headers)[0] == 400
            status, _ = request(edge_port, "/", {"type": "climate", "t": 22.5, "h": 50}, device_headers)
            assert status == 200
            rpc("manage_stock", {"p_action": "add", "p_data": {"uid": "127C1207", "name": "ESP32 tag", "stock": 5, "low": 3}})
            event = {"type": "scan", "uid": "127C1207", "event_id": str(uuid.uuid4())}
            status, first = request(edge_port, "/", event, device_headers)
            assert status == 200 and first["ok"] and not first["duplicate"]
            with ThreadPoolExecutor(max_workers=4) as pool:
                retries = list(pool.map(lambda _: request(edge_port, "/", event, device_headers), range(4)))
            assert all(status == 200 and data["duplicate"] for status, data in retries)
            state = rpc("get_state", {})
            assert state["products"][0]["stock"] == 4
            assert state["products"][0]["uid"] == "12:7C:12:07"
            assert state["climate"]["t"] == 22.5 and state["climate"]["h"] == 50
            assert state["device"] == "online"
            assert len(state["scans"]) == 1
            assert len(rpc("get_history", {"p_hours": 24})) == 1
            assert request(rest_port, "/rpc/ingest_device", {"p_event": event}, member_headers)[0] in (401, 403)
            print("Device header auth and payload validation passed.")
            print("ESP32-format climate and scan events reached the real Edge Function, PostgREST, and PostgreSQL.")
            print("Dashboard state/history contain the readings; concurrent scan retries decrement stock only once.")
            print("Browser-role direct device ingestion was rejected.")
    finally:
        if edge_process and edge_process.poll() is None:
            os.killpg(edge_process.pid, signal.SIGTERM)
            edge_process.wait(timeout=10)
        if gateway:
            gateway.shutdown()
            gateway.server_close()
        command("rm", "--force", "--volumes", rest, database, check=False)
        command("network", "rm", network, check=False)


if __name__ == "__main__":
    main()
