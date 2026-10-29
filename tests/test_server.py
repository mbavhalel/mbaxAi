"""HTTP integration tests. Every server runs against its own temporary database.

Run: python -m unittest discover -s tests -v
"""
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "integration-test-password-only"
AUTH = "Basic " + base64.b64encode(("admin:" + TEST_PASSWORD).encode()).decode()


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="storeroom-test-")
        self.addCleanup(self.directory.cleanup)
        self.db_path = Path(self.directory.name) / "stock.db"
        self.log_path = Path(self.directory.name) / "server.log"
        self.process = None
        self.addCleanup(self.stop_server)
        self.start_server()

    def start_server(self):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        environment = os.environ.copy()
        environment.update(
            APP_USERNAME="admin", APP_PASSWORD=TEST_PASSWORD,
            HTTP_HOST="127.0.0.1", HTTP_PORT=str(self.port), PORT=str(self.port),
            DB_PATH=str(self.db_path), MQTT_ENABLED="false",
            TG_TOKEN="", TG_CHAT_ID="",
        )
        with self.log_path.open("ab") as log:
            self.process = subprocess.Popen(
                [sys.executable, "server.py"], cwd=ROOT, env=environment,
                stdout=log, stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.fail("Server exited during startup:\n" + self.log_path.read_text())
            try:
                if self.request("GET", "/healthz", authenticated=False)[0] == 200:
                    return
            except (OSError, http.client.HTTPException):
                pass
            time.sleep(0.05)
        self.fail("Server did not become ready:\n" + self.log_path.read_text())

    def stop_server(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def request(self, method, path, data=None, *, authenticated=True,
                headers=None, raw=None):
        request_headers = {"Authorization": AUTH} if authenticated else {}
        if data is not None:
            raw = json.dumps(data).encode()
            request_headers["Content-Type"] = "application/json"
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.request(method, path, body=raw, headers=request_headers)
            response = connection.getresponse()
            return response.status, {key.lower(): value for key, value in response.getheaders()}, response.read()
        finally:
            connection.close()

    def state(self):
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200, body)
        return json.loads(body)

    def add_product(self, stock=10):
        status, _, body = self.request("POST", "/api/products", {
            "uid": "04:A1:B2:C3", "name": "Apples", "stock": stock, "low": 2,
        })
        self.assertEqual(status, 200, body)

    def test_health_has_no_private_data(self):
        status, _, body = self.request("GET", "/healthz", authenticated=False)
        self.assertEqual(status, 200)
        for private_field in (b"products", b"scans", b"settings", str(self.db_path).encode()):
            self.assertNotIn(private_field, body)

    def test_authentication_protects_every_data_surface(self):
        endpoints = (
            ("GET", "/"), ("GET", "/index.html"),
            ("GET", "/api/state"), ("GET", "/api/history"),
            ("GET", "/api/events"), ("GET", "/export/readings.csv"),
            ("GET", "/export/scans.csv"), ("POST", "/api/products"),
            ("PATCH", "/api/products/04:A1:B2:C3"),
            ("DELETE", "/api/products/04:A1:B2:C3"),
            ("PUT", "/api/settings"), ("POST", "/api/clear"),
        )
        for method, path in endpoints:
            with self.subTest(method=method, path=path):
                status, headers, _ = self.request(method, path, authenticated=False)
                self.assertEqual(status, 401)
                self.assertTrue(headers.get("www-authenticate", "").startswith("Basic "))
        wrong = "Basic " + base64.b64encode(b"admin:wrong-password").decode()
        self.assertEqual(self.request("GET", "/api/state", headers={"Authorization": wrong})[0], 401)
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Storeroom monitor", body)

    def test_stock_settings_exports_and_restart_persistence(self):
        self.add_product()
        uid_path = "/api/products/04%3AA1%3AB2%3AC3"
        self.assertEqual(self.request("PATCH", uid_path, {"stock": 7, "low": 3})[0], 200)
        settings = {"tMin": 12, "tMax": 27, "hMin": 25, "hMax": 65}
        self.assertEqual(self.request("PUT", "/api/settings", settings)[0], 200)
        current = self.state()
        self.assertEqual(current["products"][0]["stock"], 7)
        self.assertEqual(current["products"][0]["low"], 3)
        self.assertEqual(current["settings"], settings)
        self.assertEqual(current["scans"][0]["kind"], "manual")
        status, headers, csv = self.request("GET", "/export/scans.csv")
        self.assertEqual(status, 200)
        self.assertIn("text/csv", headers["content-type"])
        self.assertIn(b"Apples", csv)
        self.stop_server()
        self.start_server()
        restored = self.state()
        self.assertEqual(restored["products"], current["products"])
        self.assertEqual(restored["settings"], settings)
        self.assertEqual(restored["scans"], current["scans"])
        self.assertEqual(self.request("DELETE", uid_path)[0], 200)
        self.assertEqual(self.state()["products"], [])

    def test_sse_delivers_initial_state_and_live_edits(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        self.addCleanup(connection.close)
        connection.request("GET", "/api/events", headers={"Authorization": AUTH})
        response = connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertIn("text/event-stream", response.getheader("Content-Type"))

        def next_state():
            # Each iteration consumes one line, including blank separators and comments.
            for _ in range(30):
                line = response.readline().decode().strip()
                if line.startswith("data:"):
                    return json.loads(line[5:].strip())
            self.fail("No state event arrived")

        self.assertEqual(next_state()["products"], [])
        self.add_product()
        self.assertEqual(next_state()["products"][0]["name"], "Apples")

    def test_mutations_reject_cross_site_requests(self):
        for headers in (
            {"Origin": "https://untrusted.example"},
            {"Origin": "null"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.request("POST", "/api/clear", {}, headers=headers)[0], 403)
        self.assertEqual(self.request("POST", "/api/clear", {}, headers={
            "Origin": "http://127.0.0.1:" + str(self.port),
            "Sec-Fetch-Site": "same-origin",
        })[0], 200)

    def test_json_requirements_and_request_size_limit(self):
        self.assertIn(self.request("POST", "/api/products", raw=b"{}")[0], (400, 415))
        self.assertEqual(self.request("POST", "/api/products", raw=b"{broken", headers={
            "Content-Type": "application/json",
        })[0], 400)
        self.assertEqual(self.request("POST", "/api/products", data=[])[0], 400)
        self.assertEqual(self.request("POST", "/api/products", raw=b" " * (65536 + 1), headers={
            "Content-Type": "application/json",
        })[0], 413)
        self.assertEqual(self.state()["products"], [])


class ConfigurationTests(unittest.TestCase):
    def test_missing_or_short_password_fails_before_database_creation(self):
        with tempfile.TemporaryDirectory(prefix="storeroom-config-test-") as directory:
            db_path = Path(directory) / "must-not-exist.db"
            for password in (None, "short"):
                with self.subTest(password_present=password is not None):
                    environment = os.environ.copy()
                    environment.pop("APP_PASSWORD", None)
                    environment.update(DB_PATH=str(db_path), MQTT_ENABLED="false")
                    if password is not None:
                        environment["APP_PASSWORD"] = password
                    result = subprocess.run(
                        [sys.executable, "server.py"], cwd=ROOT, env=environment,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"APP_PASSWORD", result.stdout)
                    self.assertFalse(db_path.exists())


class MqttTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="storeroom-mqtt-test-")
        self.addCleanup(self.directory.cleanup)
        spec = importlib.util.spec_from_file_location("storeroom_test_app", ROOT / "server.py")
        self.server = importlib.util.module_from_spec(spec)
        with mock.patch.dict(os.environ, {
            "APP_PASSWORD": TEST_PASSWORD, "APP_USERNAME": "admin",
            "DB_PATH": str(Path(self.directory.name) / "stock.db"),
            "MQTT_ENABLED": "false", "TG_TOKEN": "", "TG_CHAT_ID": "",
        }), mock.patch.dict(sys.modules, {"storeroom_test_app": self.server}):
            spec.loader.exec_module(self.server)
        self.addCleanup(self.server.db.close)

    def test_mqtt_credentials_and_verified_tls_are_configured(self):
        client = mock.Mock()
        with mock.patch.object(self.server.mqtt, "Client", return_value=client) as constructor, \
                mock.patch.multiple(self.server, MQTT_USERNAME="broker-user",
                                    MQTT_PASSWORD="broker-test-password", MQTT_TLS=True,
                                    MQTT_CLIENT_ID="test-server"):
            self.assertIs(self.server.make_client(), client)
            constructor.assert_called_once_with(
                self.server.mqtt.CallbackAPIVersion.VERSION2, client_id="test-server")
            client.username_pw_set.assert_called_once_with("broker-user", "broker-test-password")
            client.tls_set.assert_called_once_with()
            client.tls_insecure_set.assert_not_called()
            client.reconnect_delay_set.assert_called_once_with(1, 30)
            self.assertIs(client.on_message, self.server.on_message)

    def test_local_broker_does_not_require_tls_or_credentials(self):
        client = mock.Mock()
        with mock.patch.object(self.server.mqtt, "Client", return_value=client), \
                mock.patch.multiple(self.server, MQTT_USERNAME="", MQTT_PASSWORD="", MQTT_TLS=False):
            self.server.make_client()
        client.tls_set.assert_not_called()
        client.username_pw_set.assert_not_called()

    def test_malformed_mqtt_messages_and_invalid_tag_ids_are_ignored(self):
        for topic in ("storeroom/climate", "storeroom/scan"):
            for payload in (b"not-json", b"null", b"[]", b"42", b'"text"', b"{}"):
                with self.subTest(topic=topic, payload=payload):
                    self.server.on_message(None, None, SimpleNamespace(topic=topic, payload=payload))
        for uid in ("", "not a tag", "04:A1:B2", "04:A1:B2:C3'", "<script>alert(1)</script>"):
            with self.subTest(uid=uid):
                self.server.handle_scan({"uid": uid})
        self.assertEqual(self.server.db.execute("SELECT COUNT(*) FROM scans").fetchone()[0], 0)
        self.assertEqual(self.server.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)

    def test_valid_scan_normalizes_tag_and_decrements_stock(self):
        self.server.add_product({"uid": "04:A1:B2:C3", "name": "Apples", "stock": 5, "low": 1})
        self.server.on_message(None, None, SimpleNamespace(
            topic="storeroom/scan", payload=b'{"uid":"04a1b2c3"}'))
        state = self.server.state()
        self.assertEqual(state["products"][0]["stock"], 4)
        self.assertEqual(state["scans"][0]["uid"], "04:A1:B2:C3")
        self.assertEqual(state["scans"][0]["after"], 4)

    def test_nonfinite_climate_and_settings_cannot_reach_database(self):
        for bad in (float("nan"), float("inf"), float("-inf"), "NaN", "Infinity", 10 ** 1000):
            with self.subTest(bad=str(bad)[:30]):
                self.server.handle_climate({"t": bad, "h": 50})
                self.server.handle_climate({"t": 22, "h": bad})
                settings = {"tMin": bad, "tMax": 30, "hMin": 20, "hMax": 70}
                with self.assertRaises(ValueError):
                    self.server.save_settings(settings)
        self.assertEqual(self.server.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 0)
        self.assertEqual(self.server.get_settings(), self.server.DEFAULTS)
        self.server.handle_climate({"t": 22.5, "h": 48})
        self.assertEqual(self.server.state()["climate"]["t"], 22.5)
        self.assertEqual(self.server.db.execute("SELECT COUNT(*) FROM readings").fetchone()[0], 1)


class BackupTests(unittest.TestCase):
    def test_backup_includes_committed_wal_and_never_overwrites_files(self):
        with tempfile.TemporaryDirectory(prefix="storeroom-backup-test-") as directory:
            source = Path(directory) / "source.db"
            destination = Path(directory) / "backup.db"
            connection = sqlite3.connect(source)
            self.addCleanup(connection.close)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("CREATE TABLE stock(name TEXT, quantity INTEGER)")
            connection.execute("INSERT INTO stock VALUES('Apples', 7)")
            connection.commit()
            self.assertGreater(Path(str(source) + "-wal").stat().st_size, 0)

            def backup(target):
                return subprocess.run(
                    [sys.executable, "backup.py", str(target), "--source", str(source)],
                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=5,
                )

            result = backup(destination)
            self.assertEqual(result.returncode, 0, result.stdout.decode())
            with sqlite3.connect(destination) as restored:
                self.assertEqual(restored.execute("SELECT * FROM stock").fetchall(), [("Apples", 7)])
            original_bytes = destination.read_bytes()
            result = backup(destination)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(destination.read_bytes(), original_bytes)
            # A mistaken destination equal to the live source must also be refused.
            self.assertNotEqual(backup(source).returncode, 0)
            self.assertEqual(connection.execute("SELECT * FROM stock").fetchall(), [("Apples", 7)])


if __name__ == "__main__":
    unittest.main()
