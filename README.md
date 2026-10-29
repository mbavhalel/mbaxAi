# mbaxAi — Storeroom monitor

Open one HTTPS address on your phone or laptop to manage stock and see live
temperature, humidity, and scans. The cloud server keeps running when your laptop
is off. The dashboard is already sized for phone screens; no mobile app is needed.

## Free cloud hosting

This application needs an **always-on machine and persistent storage**. A sleeping
free web service will miss MQTT scans and delay alerts; an ephemeral filesystem
will lose stock data. Keep one server instance running.

A possible zero-cost setup is an **Oracle Cloud Always Free VM**, a free
**DuckDNS hostname**, and an authenticated **MQTT broker with a free allowance**
(for example HiveMQ Cloud, if available). Check current provider terms and
capacity before signing up. Free VMs may require card verification, may be
unavailable or reclaimed when idle, and have no uptime guarantee. Avoid paid
resources and upgrades if zero cost is essential. This repository does not
create cloud accounts or provision a VM.

### Deploy

Push these code changes to your repository before cloning it on the VM.

1. Create an eligible free Ubuntu VM with a public IP. Install Docker Engine and
   the Compose plugin using [Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/).
   Allow inbound TCP **80 and 443** in the provider firewall and Ubuntu firewall.
   Keep SSH access restricted to your own IP. Do not expose port 8080 or SQLite.
2. Point a hostname at that IP. You can use a free subdomain from
   [DuckDNS](https://www.duckdns.org/); keep its IP record updated if the IP changes.
3. On the VM, download this project and create your configuration:

   ```sh
   git clone https://github.com/mbavhalel/mbaxAi.git
   cd mbaxAi
   cp .env.example .env
   chmod 600 .env
   python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
   nano .env
   ```

   In `.env`, set `DOMAIN` to your hostname without `https://`, paste the generated
   password into `APP_PASSWORD`, and enter your MQTT broker's host, username, and
   password. The dashboard login name is `admin`. Keep `.env` private.

4. Start it:

   ```sh
   sudo docker compose up -d --build
   sudo docker compose ps
   sudo docker compose logs --tail=50
   ```

   Open `https://YOUR_HOSTNAME` on either device and sign in. Caddy obtains and
   renews the HTTPS certificate automatically. DNS must point to the VM and both
   ports must be reachable for this to work. Use HTTPS for every remote login.
   The app and proxy restart automatically after a VM reboot while Docker is enabled.

### Connect the ESP32

The ESP32 and cloud app must connect to the **same internet-reachable MQTT broker**.
The cloud server cannot connect to a broker at `localhost` on your laptop.
Configure the ESP32 with the broker hostname, TLS port (usually `8883`), username,
password, and certificate verification. Set the matching values in `.env`.
ESP32 firmware is not included in this repository, so that device change must be
made in your firmware. Keep the ESP32 powered and connected to Wi-Fi.

Keep the existing topics and payloads:

| Topic | Device publishes |
| --- | --- |
| `storeroom/climate` | `{"t":22.5,"h":50}` |
| `storeroom/scan` | `{"uid":"04:A1:B2:C3"}` |
| `storeroom/status` | `online` or `offline` (retained; use a last will for offline) |

Use a private broker or topic permissions; do not use a shared anonymous public
broker. Give the ESP32 a different client ID from `storeroom-server`.
Stop the old laptop server before running the cloud copy; two servers must not
process the same scans.
The ESP32 can keep subscribing to `storeroom/alert` for forwarding alerts.
Alternatively, set `TG_TOKEN` and `TG_CHAT_ID` in `.env` to send Telegram alerts
directly from the server. Restart with `sudo docker compose up -d` after config changes.

## Saved stock and backups

Docker stores the live database in the `stock_data` volume, outside the app image.
Rebuilding or restarting the app preserves it. **Do not run `docker compose down -v`**;
that deletes the data volumes. Exported CSVs are useful reports, but a database
backup also preserves settings and registered products.

Create a consistent backup, including committed data still in SQLite's WAL:

```sh
sudo docker compose exec app python backup.py /data/backup.db
sudo docker compose cp app:/data/backup.db ./backup.db
sudo docker compose exec app rm /data/backup.db
```

Copy `backup.db` somewhere outside the VM. Repeat regularly; the script refuses to
overwrite an existing backup. To restore, stop the app, remove its old WAL/SHM
sidecars, and replace `/data/storeroom.db` with the backup before starting it.
The restored file must be writable by the container's UID/GID `10001:10001`.
Never replace a database while the app is running.

The database files already committed to this repository are legacy snapshots.
They are excluded from Docker builds and are **not imported automatically**.
To preserve that saved stock, run `python3 backup.py existing-stock.db --source storeroom.db`
in the old checkout while its database and sidecars are together, then restore
that backup on the stopped cloud app. Review those tracked snapshots for private
data before sharing the repository publicly.

## Local development

Python 3.10 or newer is required. Install dependencies in a virtual environment:

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

Set `APP_PASSWORD` (at least 16 characters) in your terminal environment, then run
`python server.py`. On Windows, activate `.venv\Scripts\Activate.ps1` and use
`$env:APP_PASSWORD = 'your-long-local-password'` in PowerShell; on macOS/Linux use
`export APP_PASSWORD='your-long-local-password'`. Open `127.0.0.1:8080` locally
and sign in as `admin`. `.env` is read by Docker Compose; direct Python runs use
the terminal's environment instead.

The default local database is `~/.mbaxai/storeroom.db`, so developing does not
change the committed database snapshots. Set `DB_PATH` to choose another path.
`MQTT_ENABLED=false` lets you develop the dashboard without a broker. For local
MQTT, set `MQTT_HOST` and `MQTT_PORT`; use `MQTT_TLS=true` for verified TLS.
`HTTP_HOST` defaults to loopback; `HTTP_PORT` defaults to 8080 (`PORT` overrides
it for hosting platforms). The Docker deployment sets the correct bind address.

Run the tests:

```sh
python -m unittest discover -s tests -v
```

Tests use temporary databases and never change the saved stock. They cover login,
stock changes, restart persistence, CSV exports, live events, and request validation.
