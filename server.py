#!/usr/bin/env python3
"""
Storeroom server
  - listens to the ESP32 over MQTT (climate, scans, status)
  - stores everything in SQLite (storeroom.db, one file next to this script)
  - keeps the stock counts and sends low / out-of-stock alerts, even with no browser open
  - serves the dashboard and a small JSON API on http://localhost:8080

Run:   pip install paho-mqtt
       python server.py

Optional environment variables:
  MQTT_HOST (localhost)  MQTT_PORT (1883)  HTTP_HOST (127.0.0.1)  HTTP_PORT (8080)
  DB_PATH (storeroom.db)  KEEP_DAYS (365: how long to keep temperature readings)
  TG_TOKEN + TG_CHAT_ID   send stock alerts straight to Telegram. If not set, alerts are
                          published on MQTT and the ESP32 forwards them to Telegram.
"""
import csv, io, json, os, queue, re, sqlite3, threading, time, urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
HTTP_HOST = os.getenv("HTTP_HOST", "127.0.0.1")   # use 0.0.0.0 to open it from other devices (no login!)
HTTP_PORT = int(os.getenv("HTTP_PORT", "8080"))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", os.path.join(HERE, "storeroom.db"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "365"))
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS readings(ts INTEGER PRIMARY KEY, temp REAL, hum REAL);
CREATE TABLE IF NOT EXISTS products(uid TEXT PRIMARY KEY, name TEXT NOT NULL, stock INTEGER NOT NULL,
                                    low INTEGER NOT NULL, alerted TEXT NOT NULL DEFAULT 'ok');
CREATE TABLE IF NOT EXISTS scans(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, kind TEXT,
                                 uid TEXT, name TEXT, stock_after INTEGER);
CREATE TABLE IF NOT EXISTS ignored(uid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value REAL);
""")
lock = threading.RLock()
alert_lock = threading.Lock()
DEFAULTS = {"tMin": 15, "tMax": 25, "hMin": 30, "hMax": 60}

latest = {"t": None, "h": None, "ts": 0}
row = db.execute("SELECT ts,temp,hum FROM readings ORDER BY ts DESC LIMIT 1").fetchone()
if row:
    latest = {"ts": row[0], "t": row[1], "h": row[2]}
device = "unknown"
mqtt_ok = False
client = None


def clamp_int(v, default=0):
    try:
        return max(0, min(1_000_000, int(float(v))))
    except (TypeError, ValueError):
        return default


def norm_uid(raw):
    """'04a1b2c3', '04 A1 B2 C3' and '04:a1:b2:c3' all become '04:A1:B2:C3'. Returns None if it isn't a tag ID."""
    h = re.sub(r"[^0-9A-Fa-f]", "", str(raw)).upper()
    if len(h) not in (8, 14, 20) or re.search(r"[^0-9A-Fa-f:\s_-]", str(raw)):
        return None
    return ":".join(h[i:i + 2] for i in range(0, len(h), 2))


def get_settings():
    s = dict(DEFAULTS)
    for r in db.execute("SELECT key,value FROM settings"):
        if r["key"] in s:
            s[r["key"]] = r["value"]
    return s


def state():
    with lock:
        products = [dict(r) for r in db.execute("SELECT uid,name,stock,low FROM products ORDER BY name COLLATE NOCASE")]
        pending = [r[0] for r in db.execute(
            "SELECT uid FROM scans WHERE kind='scan' AND stock_after IS NULL "
            "AND uid NOT IN (SELECT uid FROM products) AND uid NOT IN (SELECT uid FROM ignored) "
            "GROUP BY uid ORDER BY MAX(id) DESC")]
        scans = [dict(r) for r in db.execute(
            "SELECT ts,kind,uid,name,stock_after AS after FROM scans ORDER BY id DESC LIMIT 15")]
        settings = get_settings()
    age = round(time.time() - latest["ts"], 1) if latest["ts"] else None
    return {"climate": {"t": latest["t"], "h": latest["h"], "age": age}, "device": device, "mqtt": mqtt_ok,
            "products": products, "pending": pending, "scans": scans, "settings": settings}


def history(hours):
    hours = max(1, min(hours, 24 * 365))
    bucket = max(5, int(hours * 3600 / 300))
    since = int(time.time() - hours * 3600)
    with lock:
        rows = db.execute("SELECT (ts/?)*? AS ts, AVG(temp) AS t, AVG(hum) AS h FROM readings "
                          "WHERE ts>=? GROUP BY 1 ORDER BY 1", (bucket, bucket, since)).fetchall()
    return [{"ts": r["ts"], "t": round(r["t"], 1), "h": round(r["h"], 1)} for r in rows]


def export_csv(name):
    with lock:
        if name == "readings":
            head = ["time", "temperature_c", "humidity_pct"]
            rows = db.execute("SELECT ts,temp,hum FROM readings ORDER BY ts").fetchall()
        elif name == "scans":
            head = ["time", "kind", "tag", "item", "stock_after"]
            rows = db.execute("SELECT ts,kind,uid,name,stock_after FROM scans ORDER BY id").fetchall()
        else:
            return None
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(head)
    for r in rows:
        w.writerow([datetime.fromtimestamp(r[0]).strftime("%Y-%m-%d %H:%M:%S"), *r[1:]])
    return out.getvalue().encode("utf-8")

# ---------------------------------------------------------------- live updates (server-sent events)
clients = set()
clients_lock = threading.Lock()


def broadcast(event, data):
    msg = "event: %s\ndata: %s\n\n" % (event, json.dumps(data))
    with clients_lock:
        for q in list(clients):
            try:
                q.put_nowait(msg)
            except queue.Full:
                pass

# ---------------------------------------------------------------- stock alerts
RANK = {"ok": 0, "low": 1, "out": 2}


def level(p):
    return "out" if p["stock"] <= 0 else ("low" if p["stock"] <= p["low"] else "ok")


def send_alert(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            req = urllib.request.Request(
                "https://api.telegram.org/bot%s/sendMessage" % TG_TOKEN,
                json.dumps({"chat_id": TG_CHAT, "text": msg}).encode(),
                {"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10).read()
            print("alert sent (Telegram):", msg)
            return True
        except Exception as e:
            print("Telegram alert failed:", e)
            return False
    if not mqtt_ok or client is None:
        return False
    ok = client.publish("storeroom/alert", msg).rc == 0
    if ok:
        print("alert sent (via ESP32):", msg)
    return ok


def check_stock():
    """One alert each time an item gets worse (ok -> low -> out). Unsent alerts are retried on the next check."""
    with alert_lock:
        todo = []
        with lock:
            for p in db.execute("SELECT uid,name,stock,low,alerted FROM products").fetchall():
                l, a = level(p), p["alerted"]
                if l == a:
                    continue
                if RANK[l] < RANK[a]:
                    db.execute("UPDATE products SET alerted=? WHERE uid=?", (l, p["uid"]))
                    continue
                msg = ("ALERT: %s is out of stock" % p["name"]) if l == "out" else \
                      ("ALERT: %s is running low, only %d left" % (p["name"], p["stock"]))
                todo.append((p["uid"], l, msg))
            db.commit()
        for uid, l, msg in todo:
            if send_alert(msg):
                with lock:
                    db.execute("UPDATE products SET alerted=? WHERE uid=?", (l, uid))
                    db.commit()


def changed():
    check_stock()
    broadcast("state", state())

# ---------------------------------------------------------------- MQTT
def handle_climate(j):
    global latest
    try:
        t, h = float(j["t"]), float(j["h"])
    except (KeyError, TypeError, ValueError):
        return
    now = int(time.time())
    with lock:
        db.execute("INSERT OR REPLACE INTO readings(ts,temp,hum) VALUES(?,?,?)", (now, t, h))
        db.commit()
        latest = {"ts": now, "t": t, "h": h}
    broadcast("climate", {"t": t, "h": h, "age": 0})


def handle_scan(j):
    raw = str(j.get("uid", "")).strip()
    uid = norm_uid(raw) or raw.upper()
    if not uid:
        return
    now = int(time.time())
    with lock:
        p = db.execute("SELECT * FROM products WHERE uid=?", (uid,)).fetchone()
        if p:
            new = max(0, p["stock"] - 1)
            db.execute("UPDATE products SET stock=? WHERE uid=?", (new, uid))
            db.execute("INSERT INTO scans(ts,kind,uid,name,stock_after) VALUES(?,?,?,?,?)",
                       (now, "scan", uid, p["name"], new))
        else:
            db.execute("INSERT INTO scans(ts,kind,uid,name,stock_after) VALUES(?,?,?,?,NULL)",
                       (now, "scan", uid, "Unregistered tag"))
            db.execute("DELETE FROM ignored WHERE uid=?", (uid,))
        db.commit()
    changed()


def on_connect(c, userdata, flags, rc, *extra):
    global mqtt_ok
    mqtt_ok = (rc == 0) if isinstance(rc, int) else (not rc.is_failure)
    if mqtt_ok:
        print("MQTT connected to %s:%d" % (MQTT_HOST, MQTT_PORT))
        for t in ("storeroom/climate", "storeroom/scan", "storeroom/status"):
            c.subscribe(t)
        threading.Thread(target=changed, daemon=True).start()   # send anything that was waiting
    else:
        print("MQTT connection refused:", rc)


def on_disconnect(c, userdata, *extra):
    global mqtt_ok, device
    mqtt_ok = False
    device = "unknown"
    print("MQTT disconnected, retrying...")
    broadcast("state", state())


def on_message(c, userdata, msg):
    global device
    payload = msg.payload.decode("utf-8", "replace")
    if msg.topic == "storeroom/status":
        device = payload
        broadcast("state", state())
        return
    try:
        j = json.loads(payload)
    except ValueError:
        return
    if msg.topic == "storeroom/climate":
        handle_climate(j)
    elif msg.topic == "storeroom/scan":
        handle_scan(j)

# ---------------------------------------------------------------- changes made from the dashboard
def add_product(d):
    name = str(d.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Enter an item name")
    uid = norm_uid(d.get("uid", ""))
    if not uid:
        raise ValueError("The tag ID should look like 04:A1:B2:C3 (letters and numbers 0-9, A-F)")
    with lock:
        if db.execute("SELECT 1 FROM products WHERE uid=?", (uid,)).fetchone():
            raise ValueError("That tag ID is already used by another item")
        db.execute("INSERT INTO products(uid,name,stock,low) VALUES(?,?,?,?)",
                   (uid, name, clamp_int(d.get("stock")), clamp_int(d.get("low"), 3)))
        db.commit()


def update_product(uid, d):
    with lock:
        p = db.execute("SELECT * FROM products WHERE uid=?", (uid,)).fetchone()
        if not p:
            raise KeyError(uid)
        name = (str(d["name"]).strip()[:80] if "name" in d else "") or p["name"]
        low = clamp_int(d["low"]) if "low" in d else p["low"]
        stock = clamp_int(d["stock"]) if "stock" in d else p["stock"]
        db.execute("UPDATE products SET name=?, stock=?, low=? WHERE uid=?", (name, stock, low, uid))
        if stock != p["stock"]:
            db.execute("INSERT INTO scans(ts,kind,uid,name,stock_after) VALUES(?,?,?,?,?)",
                       (int(time.time()), "manual", uid, name, stock))
        db.commit()


def delete_product(uid):
    with lock:
        db.execute("DELETE FROM products WHERE uid=?", (uid,))
        db.commit()


def save_settings(d):
    vals = {}
    for k in DEFAULTS:
        try:
            vals[k] = float(d[k])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Enter a number for every range")
    if vals["tMin"] >= vals["tMax"] or vals["hMin"] >= vals["hMax"]:
        raise ValueError("Each minimum must be lower than its maximum")
    with lock:
        for k, v in vals.items():
            db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (k, v))
        db.commit()


def clear_stock():
    with lock:
        for t in ("products", "scans", "ignored"):
            db.execute("DELETE FROM " + t)
        db.commit()

# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, ctype, code=200, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, obj):
        self.reply(json.dumps(obj).encode(), "application/json")

    def text(self, msg, code):
        self.reply(msg.encode(), "text/plain; charset=utf-8", code)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "dashboard.html"), "rb") as f:
                    return self.reply(f.read(), "text/html; charset=utf-8")
            except OSError:
                return self.text("dashboard.html must sit next to server.py", 404)
        if u.path == "/api/state":
            return self.json(state())
        if u.path == "/api/history":
            hours = clamp_int((parse_qs(u.query).get("hours") or ["24"])[0], 24) or 24
            return self.json(history(hours))
        if u.path == "/api/events":
            return self.events()
        if u.path.startswith("/export/") and u.path.endswith(".csv"):
            data = export_csv(u.path[8:-4])
            if data is None:
                return self.text("Not found", 404)
            return self.reply(data, "text/csv; charset=utf-8",
                              extra={"Content-Disposition": 'attachment; filename="%s"' % u.path[8:]})
        self.text("Not found", 404)

    def events(self):
        q = queue.Queue(maxsize=100)
        with clients_lock:
            clients.add(q)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(("retry: 3000\nevent: state\ndata: %s\n\n" % json.dumps(state())).encode())
            self.wfile.flush()
            while True:
                try:
                    msg = q.get(timeout=15)
                except queue.Empty:
                    msg = ": ping\n\n"
                self.wfile.write(msg.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with clients_lock:
                clients.discard(q)

    def dispatch(self, method):
        path = urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length") or 0)
            d = json.loads(self.rfile.read(n) or b"{}") if method in ("POST", "PUT", "PATCH") else {}
            if not isinstance(d, dict):
                raise ValueError("Bad request")
            if method == "POST" and path == "/api/products":
                add_product(d)
            elif method == "POST" and path == "/api/ignore":
                with lock:
                    db.execute("INSERT OR IGNORE INTO ignored(uid) VALUES(?)", (str(d.get("uid", "")).upper(),))
                    db.commit()
            elif method == "POST" and path == "/api/clear":
                clear_stock()
            elif method == "PUT" and path == "/api/settings":
                save_settings(d)
            elif method == "PATCH" and path.startswith("/api/products/"):
                update_product(unquote(path[14:]).upper(), d)
            elif method == "DELETE" and path.startswith("/api/products/"):
                delete_product(unquote(path[14:]).upper())
            else:
                return self.text("Not found", 404)
            changed()
            self.json({"ok": True})
        except ValueError as e:
            self.text(str(e), 400)
        except KeyError:
            self.text("Not found", 404)

    def do_POST(self): self.dispatch("POST")
    def do_PUT(self): self.dispatch("PUT")
    def do_PATCH(self): self.dispatch("PATCH")
    def do_DELETE(self): self.dispatch("DELETE")


def prune_loop():
    while True:
        with lock:
            db.execute("DELETE FROM readings WHERE ts<?", (int(time.time() - KEEP_DAYS * 86400),))
            db.commit()
        time.sleep(6 * 3600)


def make_client():
    try:
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="storeroom-server")   # paho-mqtt 2.x
    except AttributeError:
        c = mqtt.Client(client_id="storeroom-server")                                     # paho-mqtt 1.x
    c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
    c.reconnect_delay_set(1, 30)
    return c


def main():
    global client
    client = make_client()
    client.connect_async(MQTT_HOST, MQTT_PORT, 60)   # the dashboard still opens if the broker is down
    client.loop_start()
    threading.Thread(target=prune_loop, daemon=True).start()
    srv = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), Handler)
    srv.daemon_threads = True
    print("Dashboard: http://%s:%d   database: %s" % ("localhost" if HTTP_HOST in ("127.0.0.1", "0.0.0.0") else HTTP_HOST, HTTP_PORT, DB_PATH))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()