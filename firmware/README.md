# ESP32: replace MQTT publishing with HTTPS

The sensor and RFID firmware is not in this repository. `StoreroomSupabase.h`
is a helper to integrate into that firmware; it is not a complete flashable sketch.
It uses the ESP32 Arduino core, with no additional MQTT or JSON library.

1. Keep the existing Wi-Fi, sensor, and RFID code.
2. Include `StoreroomSupabase.h`. Configure this endpoint:
   `https://YOUR_PROJECT_REF.supabase.co/functions/v1/ingest-device`.
3. Provision the same `DEVICE_TOKEN` set in the Supabase function secrets. Do not
   put this token, the Supabase service key, or Telegram credentials in the website.
4. Provision the trusted root CA for your endpoint's actual certificate chain,
   and synchronize the ESP32's clock with NTP before sending. Use your firmware's
   certificate provisioning/update mechanism. Do not disable TLS verification.
5. Replace MQTT climate publishing with `client.send(StoreroomSupabase::climate(t,h))`.
   Send readings roughly every 30–60 seconds, within your plan's quotas.
6. On a physical tag scan, generate `StoreroomSupabase::scan(uid)` once and send
   that payload. Store unsuccessful scan payloads in a persistent FIFO (for example
   LittleFS or Preferences) until acknowledged. Reuse the original payload after
   reconnects and reboots; generating a new UUID for a retry counts it as a new scan.

Integration outline (use your configured endpoint, secret, and CA strings):

```cpp
#include "StoreroomSupabase.h"
StoreroomSupabase client(INGEST_URL, DEVICE_TOKEN, SUPABASE_ROOT_CA);

// Called by your existing sensor loop after Wi-Fi and NTP are ready:
client.send(StoreroomSupabase::climate(temperature, humidity));

// Called once by your RFID code for a physical scan:
String pendingScan = StoreroomSupabase::scan(tagUid);
// Persist pendingScan before sending; delete it from your queue only on 2xx.
int result = client.send(pendingScan);
if (result >= 200 && result < 300) {
  // Acknowledged; remove this exact entry from your persistent queue.
} else {
  // Retry network/429/5xx with exponential backoff using the SAME pendingScan.
  // On 400/401/403, retain it and fix the data/credentials/configuration.
}
```

You can also send `{"type":"status","status":"online"}` or `offline`.
An online device becomes offline in the dashboard after 90 seconds without a
reading/status/scan, so abrupt power loss does not need an MQTT last will.

This version does not subscribe to MQTT or forward `storeroom/alert` on the ESP32.
Optional Telegram alerts are sent by the Supabase function instead. The dashboard's
low/out-of-stock warnings work without Telegram. Keep Wi-Fi and the ESP32 powered
when your laptop is off. Hardware flashing and sensor integration need your actual
firmware and were not tested against a physical ESP32 here.
