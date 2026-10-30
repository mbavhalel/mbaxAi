# ESP32: complete RFID + DHT22 + LCD sketch

Open [`StoreroomMonitor/StoreroomMonitor.ino`](StoreroomMonitor/StoreroomMonitor.ino)
in Arduino IDE. Keep all files from that folder together. This is the complete
Supabase version of the supplied sketch, with the same wiring and LCD messages.

## 1. Install the board and libraries

In Arduino IDE, install **esp32 by Espressif Systems, version 3.3.8**, using the
Boards Manager. Its additional board-manager URL is:

```text
https://espressif.github.io/arduino-esp32/package_esp32_index.json
```

Select **ESP32 Dev Module** for a classic ESP32/WROOM board, your USB serial port,
and the default 4 MB flash/partition settings. These exact library versions were
compiled successfully:

| Library Manager name | Version |
| --- | --- |
| MFRC522 | 1.4.12 |
| LiquidCrystal I2C — Frank de Brabander | 1.1.2 |
| DHT sensor library — Adafruit | 1.4.7 |
| Adafruit Unified Sensor | 1.1.15 |
| ArduinoJson | 7.4.3 |

WiFi, HTTPS, LittleFS, and Preferences come with the ESP32 board package.

## 2. Set four connection values

Copy `StoreroomMonitor/secrets.example.h` to **`secrets.h` in the same folder**.
Fill these quoted strings; do not send the private values in chat or commit them:

```cpp
constexpr char WIFI_SSID[] = "YOUR_2_4_GHZ_WIFI_NAME";
constexpr char WIFI_PASSWORD[] = "YOUR_WIFI_PASSWORD";
constexpr char INGEST_URL[] = "https://YOUR_PROJECT_REF.supabase.co/functions/v1/ingest-device";
constexpr char DEVICE_TOKEN[] = "THE_SAME_DEVICE_TOKEN_SET_IN_SUPABASE";
```

The project URL comes from Supabase's Connect/API settings. It looks like
`https://abcdefgh.supabase.co`; it is not the path to the SQL migration file.
The example is already filled with your supplied project's endpoint:
`https://uijggknglvidlyakhupx.supabase.co/functions/v1/ingest-device`.
The device token must be the same random value of at least 32 characters used by
the `ingest-device` function. No Supabase publishable, secret, or service-role key
is needed in the firmware. `secrets.h` is ignored by Git.

Leave `ROOT_CA` at its supplied default. `TrustedRoots.h` contains a small public
CA bundle for verified HTTPS, with fingerprints recorded in the file. NTP supplies
the clock needed for certificate validation. If a project's certificate issuer
changes to a root outside the bundle, update the verified public CA bundle or
configure its trusted root PEM; do not disable verification or use `setInsecure()`.

Before flashing, complete the [Supabase setup in the main guide](../README.md#set-it-up),
including deploying `ingest-device` and setting its `DEVICE_TOKEN` secret.

## 3. Upload and initialize the queue once

Upload the sketch, keeping your existing private `secrets.h`, then open Serial
Monitor at **115200 baud**. The new startup banner says **Queue setup v2**.
On an uninitialized filesystem, send this exact text through Serial Monitor:

```text
FORMAT_QUEUE
```

There is **no startup deadline**. Newline, CRLF, and “No line ending” all work;
with no line ending, the command runs after a short pause. Wait for:

```text
Empty queue initialized and mounted. Scans can now be saved.
```

The queue becomes usable immediately; no reset or re-upload is required after
that message. A successful scan then reports `RFID scan saved locally` followed
by `Scan acknowledged` when Supabase accepts it.

Formatting is explicit and is **refused when the filesystem is already mounted**.
Only initialize a new/known-empty LittleFS partition; formatting an unmounted
partition that previously contained files can erase them. Existing data is never
erased automatically. `RETRY_QUEUE` instead retries mounting/opening storage
without formatting, and is suitable when trying to recover existing files.

When upgrading the old NVS-only sketch, LittleFS may still be new/unformatted.
Initializing a known-empty LittleFS partition does not erase the separate NVS
`mbaxscan` records; the sketch copies those records after the filesystem mounts.
If initialization fails, Serial Monitor now identifies whether the failure was
mounting, opening NVS metadata, opening a file, writing, or renaming it.

The LCD then shows the original temperature/humidity display, alternating the
second line with cloud/queue status. It keeps showing access messages for 2.5
seconds. Wi-Fi retries, NTP synchronization, and HTTPS run in a separate task so
they do not freeze the RFID reader or LCD.

## 4. Register the tag and check communication

Remove all cards from the reader after boot. Wait at least **350 ms** before
presenting a tag. After each scan, remove all cards again before scanning another.
A tag left on the reader, including during a reboot, is not counted as a new scan.

Your example authorized UID is:

```text
127C1207  →  12:7C:12:07 in the dashboard
```

For the first scan of an unknown tag, the dashboard offers registration. Use
“Use last scanned tag” or enter `12:7C:12:07`, name the item, and set its starting
stock. Subsequent physical scans reduce that item's stock by one. Stock never
falls below zero. Climate readings upload roughly every **30 seconds** while the
DHT22 is read locally every 2 seconds. If the DHT fails, a status heartbeat still
keeps a connected RFID device online; the dashboard marks it offline after 90
seconds without any event.

`ACCESS GRANTED`/`ACCESS DENIED` retains the supplied sketch's local UID comparison.
By default, **all tags are sent for inventory**, including unknown tags that need
registration. To send only the single authorized UID, set
`SEND_ONLY_AUTHORIZED_TAGS = true` in `secrets.h`. The LCD comparison does not
operate a lock and does not replace the private Supabase device token.

## Wiring retained from your sketch

| Component | ESP32 pins/settings |
| --- | --- |
| MFRC522 RFID | SS/SDA 5, RST 27, SCK 18, MISO 19, MOSI 23 |
| DHT22 | DATA 4 |
| 16×2 I²C LCD | SDA 21, SCL 22, address `0x27` |
| Shared ground | Connect component grounds to ESP32 GND |

The MFRC522 uses 3.3 V. Keep ESP32 GPIO/I²C levels at 3.3 V as required by your
modules. This sketch adds networking; it does not change your existing pin layout.

## Offline behavior

Each scan gets one UUID and is written to LittleFS before “saved locally” is
reported. The queue holds **128 scans** in order. Pending events survive resets
and uploads that preserve the flash data partition. Do not choose “Erase All
Flash” or change partition layouts while scans are pending.

When upgrading the uploaded NVS-based sketch, existing `mbaxscan/q0` through
`q31` records are copied to the new queue with their **original UUIDs**. An old
slot is removed only after its durable copy succeeds. Full storage or invalid
records leave the old NVS data intact; copying resumes as space becomes available.
Do not erase NVS or choose “Erase All Flash” during the upgrade.

A scan is removed only after the verified HTTPS endpoint returns both a 2xx status
and JSON `{"ok":true}`. Retries keep the same UUID, so a lost response cannot
reduce stock twice. Network failures, 429, and 5xx use exponential backoff; 4xx
configuration errors retain the data for correction. Sensor uploads have a
separate schedule, so an invalid queued scan does not stop heartbeats.

If the queue is full, the LCD says `QUEUE FULL!` and the new scan is not saved.
Wait for queued scans to upload and present that tag again. `STORAGE ERROR!` means
durability could not be confirmed: inspect the queue before rescanning. Corrupt
records are preserved as `.bad` files and show `QUEUE NEEDS HELP`; they are not
silently deleted. Automatic formatting is disabled.

| Message | Next action |
| --- | --- |
| `SET secrets.h` | Fill the Wi-Fi, HTTPS endpoint, and device-token fields, then upload again. |
| `WiFi reconnect` | Check the 2.4 GHz Wi-Fi name/password and router access. |
| `Syncing clock` | Allow NTP access; HTTPS waits for a valid clock. |
| `Cloud error 401` | Match `DEVICE_TOKEN` with the function's secret, then redeploy if needed. |
| `Cloud error 404` | Check the project URL and deploy `ingest-device`. |
| `Cloud retrying` | Check internet access and the project's function logs/status. Saved scans stay queued. |
| `OLD QUEUE ERROR` | An old NVS record needs inspection; it was preserved rather than discarded. |
| `QUEUE NEEDS HELP` | Check Serial Monitor for initialization or storage details; preserve any existing queued data. |

### The TLS error in the uploaded sketch

`-9984` is an mbedTLS certificate-verification failure. The synchronized UTC clock
in your log is valid; `HTTP -1` is the transport failure that follows it, not a
DHT22 reading error. Your old `api` object uses `SUPABASE_ROOT_CA`, while the
`ESP32CertBundle` test is inside a disabled `if (false && ...)` block, so installing
that library alone does not change the certificates used for real uploads.

The complete sketch uses `TrustedRoots.h` on its actual HTTPS client and prints
safe TLS error details on failure. It also actively drains the scan queue; the
uploaded old sketch had `deliverSavedScan()` commented out. Use the complete new
folder and its matching configuration template rather than mixing old headers.
The external `ESP32CertBundle` and `SupabaseRootCA.h` files are not required.

If verification still fails after updating, keep TLS verification enabled and
send the new `HTTPS transport failed: HTTP ..., TLS ...` line. Trying a phone
hotspot can distinguish a Wi-Fi network's TLS interception from an endpoint/CA
configuration issue. Live TLS on the actual board still needs that hardware check.

## Validation

The complete sketch was compiled for **esp32:esp32:esp32** with the pinned versions
above and the default partition. Native tests cover serial setup commands, card rearming, and timer/retry
behavior. A local software integration test exercises ESP32-format requests
through the real Edge Function, PostgREST, and PostgreSQL, including duplicate
scan retries and dashboard state/history reads:

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
python3 scripts/test_device_pipeline.py
```

The integration test uses disposable Docker containers, Node/npm, and Python on
Linux. It never connects to your real project. Physical flashing, Wi-Fi, RFID
range, LCD behavior, and the DHT22 still need a check on your actual board.
