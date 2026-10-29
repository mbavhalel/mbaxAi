#!/usr/bin/env python3
"""
Storeroom server
  - listens to the ESP32 over MQTT (climate, scans, status)
  - stores everything in SQLite on a persistent data path
  - keeps the stock counts and sends low / out-of-stock alerts, even with no browser open
  - serves a password-protected dashboard and JSON API

Run: pip install -r requirements.txt, set APP_PASSWORD, then python server.py.
See README.md for free remote access and configuration.
"""
import csv, hmac, io, json, math, os, queue, re, sqlite3, threading, time, urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
from flask import Flask, Response, jsonify, request, send_file
from waitress import serve


def env_bool(name, default=False):
    value = os.getenv(name, str(default)).lower()
    if value not in ("true", "false", "1", "0"):
        raise ValueError("%s must be true or false" % name)
    return value in ("true", "1")


APP_USERNAME = os.getenv("APP_USERNAME", "admin")
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
if not APP_USERNAME or len(APP_PASSWORD) < 16:
    raise ValueError("Set APP_PASSWORD to at least 16 characters (and a nonempty APP_USERNAME). See README.md.")

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_TLS = env_bool("MQTT_TLS")
MQTT_ENABLED = env_bool("MQTT_ENABLED", True)
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883" if MQTT_TLS else "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME", "")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD", "")
MQTT_CLIENT_ID = os.getenv("MQTT_CLIENT_ID", "storeroom-server")
HTTP_HOST = os.getenv("HTTP_HOST", "127.0.0.1")
HTTP_PORT = int(os.getenv("PORT", os.getenv("HTTP_PORT", "8080")))
HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.getenv("DB_PATH", str(Path.home() / ".mbaxai" / "storeroom.db"))
KEEP_DAYS = int(os.getenv("KEEP_DAYS", "365"))
TG_TOKEN = os.getenv("TG_TOKEN", "")
TG_CHAT = os.getenv("TG_CHAT_ID", "")

# ---------------------------------------------------------------- database
Path(DB_PATH).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
DB_PATH = str(Path(DB_PATH).expanduser())
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
    except (TypeError, ValueError, OverflowError):
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
        except Exception:
            # HTTP exceptions can contain the Telegram bot token in their URL.
            print("Telegram alert failed; check the configured bot and chat.")
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
    except (KeyError, TypeError, ValueError, OverflowError):
        return
    if not math.isfinite(t) or not math.isfinite(h):
        return
    now = int(time.time())
    with lock:
        db.execute("INSERT OR REPLACE INTO readings(ts,temp,hum) VALUES(?,?,?)", (now, t, h))
        db.commit()
        latest = {"ts": now, "t": t, "h": h}
    broadcast("climate", {"t": t, "h": h, "age": 0})


def handle_scan(j):
    uid = norm_uid(j.get("uid", ""))
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
    if not isinstance(j, dict):
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
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError("Enter a number for every range")
        if not math.isfinite(vals[k]):
            raise ValueError("Enter a finite number for every range")
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
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


@app.before_request
def protect_dashboard():
    # Data-free endpoint used by the host's health checker.
    if request.path == "/healthz" and request.method in ("GET", "HEAD"):
        return None
    auth = request.authorization
    if not (auth and auth.type.lower() == "basic"
            and hmac.compare_digest((auth.username or "").encode(), APP_USERNAME.encode())
            and hmac.compare_digest((auth.password or "").encode(), APP_PASSWORD.encode())):
        return Response("Sign in to Storeroom monitor.", 401,
                        {"WWW-Authenticate": 'Basic realm="Storeroom", charset="UTF-8"'})
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # Browser Basic auth is sent automatically. Reject cross-site writes and
        # require JSON so ordinary cross-site HTML forms cannot change stock.
        origin = request.headers.get("Origin")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            return Response("Cross-site request rejected", 403)
        if origin:
            parsed = urlsplit(origin)
            # TLS terminates at the reverse proxy; never trust a forwarded host.
            if parsed.scheme not in ("http", "https") or parsed.netloc != request.host:
                return Response("Cross-site request rejected", 403)
        if request.method != "DELETE" and not request.is_json:
            return Response("Use application/json", 415)
    return None


@app.after_request
def security_headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    return response


@app.get("/healthz")
def health():
    with lock:
        db.execute("SELECT 1").fetchone()
    return jsonify(status="ok")


@app.get("/")
@app.get("/index.html")
def dashboard():
    return send_file(os.path.join(HERE, "dashboard.html"))


@app.get("/api/state")
def api_state():
    return jsonify(state())


@app.get("/api/history")
def api_history():
    return jsonify(history(clamp_int(request.args.get("hours", "24"), 24) or 24))


@app.get("/export/<name>.csv")
def download_csv(name):
    data = export_csv(name)
    if data is None:
        return Response("Not found", 404)
    return Response(data, content_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="%s.csv"' % name})


@app.get("/api/events")
def events():
    def stream():
        q = queue.Queue(maxsize=100)
        with clients_lock:
            clients.add(q)
        try:
            yield "retry: 3000\nevent: state\ndata: %s\n\n" % json.dumps(state())
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": ping\n\n"
        finally:
            with clients_lock:
                clients.discard(q)
    return Response(stream(), mimetype="text/event-stream",
                    headers={"X-Accel-Buffering": "no"})


@app.route("/api/products", methods=["POST"])
@app.route("/api/products/<uid>", methods=["PATCH", "DELETE"])
@app.route("/api/ignore", methods=["POST"])
@app.route("/api/clear", methods=["POST"])
@app.route("/api/settings", methods=["PUT"])
def change_stock(uid=None):
    d = {} if request.method == "DELETE" and not request.content_length else request.get_json(silent=True)
    if not isinstance(d, dict):
        return Response("Use a JSON object", 400)
    try:
        if request.method == "PATCH":
            update_product(uid.upper(), d)
        elif request.method == "DELETE":
            delete_product(uid.upper())
        elif request.path == "/api/products":
            add_product(d)
        elif request.path == "/api/ignore":
            tag = norm_uid(d.get("uid", ""))
            if not tag:
                raise ValueError("Invalid tag ID")
            with lock:
                db.execute("INSERT OR IGNORE INTO ignored(uid) VALUES(?)", (tag,))
                db.commit()
        elif request.path == "/api/clear":
            clear_stock()
        elif request.path == "/api/settings":
            save_settings(d)
        changed()
        return jsonify(ok=True)
    except ValueError as e:
        return Response(str(e), 400)
    except KeyError:
        return Response("Not found", 404)


def prune_loop():
    while True:
        with lock:
            db.execute("DELETE FROM readings WHERE ts<?", (int(time.time() - KEEP_DAYS * 86400),))
            db.commit()
        time.sleep(6 * 3600)


def make_client():
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=MQTT_CLIENT_ID)
    if MQTT_USERNAME:
        c.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    if MQTT_TLS:
        c.tls_set()  # System CA trust and hostname verification remain enabled.
    c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message
    c.reconnect_delay_set(1, 30)
    return c


def main():
    global client
    if MQTT_ENABLED:
        client = make_client()
        client.connect_async(MQTT_HOST, MQTT_PORT, 60)
        client.loop_start()
    threading.Thread(target=prune_loop, daemon=True).start()
    print("Dashboard listening on %s:%d" % (HTTP_HOST, HTTP_PORT), flush=True)
    try:
        # One process owns MQTT + SQLite; threads allow multiple live dashboards.
        serve(app, host=HTTP_HOST, port=HTTP_PORT, threads=16,
              max_request_body_size=64 * 1024, channel_timeout=120)
    except KeyboardInterrupt:
        pass
    finally:
        if client is not None:
            client.disconnect()
            client.loop_stop()


if __name__ == "__main__":
    main()
