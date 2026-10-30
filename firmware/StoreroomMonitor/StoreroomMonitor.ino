// RFID + DHT22 + LCD with durable, authenticated Supabase HTTPS uploads.
// Open this complete folder in Arduino IDE; fill secrets.h before uploading.
#include <Wire.h>
#include <SPI.h>
#include <MFRC522.h>
#include <LiquidCrystal_I2C.h>
#include <DHT.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <esp_random.h>
#include <time.h>
#include "PersistentScanQueue.h"
#include "BoundedResponse.h"
#include "CardGate.h"
#include "LegacyScanMigration.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

constexpr uint8_t SS_PIN = 5;
constexpr uint8_t RST_PIN = 27;
constexpr uint8_t DHT_PIN = 4;
constexpr uint32_t SENSOR_INTERVAL_MS = 2000;
constexpr uint32_t UPLOAD_INTERVAL_MS = 30000;
constexpr uint32_t WIFI_RETRY_MS = 10000;
constexpr char AUTHORISED_UID[] = "127C1207";

MFRC522 rfid(SS_PIN, RST_PIN);
LiquidCrystal_I2C lcd(0x27, 16, 2);
DHT dht(DHT_PIN, DHT22);
PersistentScanQueue scanQueue;
LegacyScanMigration legacyScanMigration;

enum class CloudState : uint8_t { NeedConfig, Connecting, Clock, Online, Retry, Rejected };
struct UploadResult { int code; bool acknowledged; };
struct SharedState {
  float temperature = 0;
  float humidity = 0;
  bool sensorOK = false;
  CloudState cloud = CloudState::Connecting;
  int httpCode = 0;
  uint8_t legacyPending = 0;
  bool legacyAttention = false;
};
SharedState shared;
portMUX_TYPE stateMutex = portMUX_INITIALIZER_UNLOCKED;

uint32_t lastSensorRead = 0;
uint32_t lastLCDUpdate = 0;
uint32_t accessMessageUntil = 0;
bool showingAccessMessage = false;
uint32_t lastCardProbe = 0;
CardGate cardGate;
bool bootRemovalMessageShown = false;

SharedState snapshot() {
  portENTER_CRITICAL(&stateMutex);
  SharedState copy = shared;
  portEXIT_CRITICAL(&stateMutex);
  return copy;
}

void cloudState(CloudState state, int code = 0) {
  portENTER_CRITICAL(&stateMutex);
  const bool changed = shared.cloud != state || shared.httpCode != code;
  shared.cloud = state;
  shared.httpCode = code;
  portEXIT_CRITICAL(&stateMutex);
  if (changed) {
    switch (state) {
      case CloudState::NeedConfig: Serial.println("Cloud disabled: fill secrets.h, including a verified root CA and long device token."); break;
      case CloudState::Connecting: Serial.println("Wi-Fi disconnected; saved scans remain queued."); break;
      case CloudState::Clock: Serial.println("Waiting for NTP clock synchronization before verified TLS."); break;
      case CloudState::Online: Serial.println("Supabase acknowledged an upload."); break;
      case CloudState::Retry: Serial.printf("Cloud request failed (%d); queued scans retained for retry.\n", code); break;
      case CloudState::Rejected: Serial.printf("Cloud rejected request (%d); check configuration/data. Queued scans retained.\n", code); break;
    }
  }
}

void lcdLine(uint8_t row, String text) {
  text = text.substring(0, 16);
  while (text.length() < 16) text += ' ';
  lcd.setCursor(0, row);
  lcd.print(text);
}

bool configured() {
  const String endpoint(DeviceConfig::INGEST_URL);
  return strlen(DeviceConfig::WIFI_SSID) > 0 &&
      strlen(DeviceConfig::DEVICE_TOKEN) >= 32 && strlen(DeviceConfig::DEVICE_TOKEN) <= 1024 &&
      endpoint.startsWith("https://") && endpoint.endsWith("/functions/v1/ingest-device") &&
      endpoint.indexOf("YOUR_PROJECT") < 0 &&
      strstr(DeviceConfig::ROOT_CA, "-----BEGIN CERTIFICATE-----") &&
      strstr(DeviceConfig::ROOT_CA, "-----END CERTIFICATE-----");
}

String eventId() {
  uint8_t bytes[16];
  esp_fill_random(bytes, sizeof(bytes));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  char result[37];
  snprintf(result, sizeof(result),
           "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
           bytes[0], bytes[1], bytes[2], bytes[3], bytes[4], bytes[5], bytes[6], bytes[7],
           bytes[8], bytes[9], bytes[10], bytes[11], bytes[12], bytes[13], bytes[14], bytes[15]);
  return String(result);
}

String scanPayload(const String& uid) {
  JsonDocument document;
  document["type"] = "scan";
  document["uid"] = uid;
  document["event_id"] = eventId();
  String payload;
  serializeJson(document, payload);
  return payload;
}

String telemetryPayload() {
  const SharedState state = snapshot();
  JsonDocument document;
  if (state.sensorOK) {
    document["type"] = "climate";
    document["t"] = state.temperature;
    document["h"] = state.humidity;
  } else {
    // A broken DHT must not make a connected RFID device appear disconnected.
    document["type"] = "status";
    document["status"] = "online";
  }
  String payload;
  serializeJson(document, payload);
  return payload;
}

UploadResult upload(const String& payload) {
  WiFiClientSecure tls;
  tls.setCACert(DeviceConfig::ROOT_CA);
  tls.setHandshakeTimeout(15);
  HTTPClient http;
  http.setConnectTimeout(10000);
  http.setTimeout(60000); // Optional Telegram processing can follow committed storage.
  http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
  if (!http.begin(tls, DeviceConfig::INGEST_URL)) return {-1, false};
  http.addHeader("Content-Type", "application/json");
  http.addHeader("X-Device-Token", DeviceConfig::DEVICE_TOKEN);
  const int code = http.POST(payload);
  if (code < 0) {
    // mbedTLS error text contains only its public diagnostic, not request
    // headers, credentials, or the URL. Useful for certificate error -9984.
    char reason[160] = {};
    const int tlsCode = tls.lastError(reason, sizeof(reason));
    Serial.printf("HTTPS transport failed: HTTP %d, TLS %d (%s)\n", code, tlsCode,
                  reason[0] ? reason : "no TLS detail");
  }
  bool acknowledged = false;
  if (code >= 200 && code < 300 && http.getSize() <= 2048) {
    // A success status alone is insufficient: only our endpoint's explicit
    // stored/deduplicated acknowledgment allows deleting a durable scan.
    BoundedResponse response;
    if (http.writeToStream(&response) >= 0 && !response.overflowed()) {
      JsonDocument document;
      if (!deserializeJson(document, response.data(), response.size()) && document["ok"].is<bool>()) {
        acknowledged = document["ok"].as<bool>();
      }
    }
  }
  http.end();
  return {code, acknowledged};
}

uint32_t retryDeadline(uint32_t& delayMs, int code) {
  delayMs = QueuePolicy::configurationRejected(code)
      ? QueuePolicy::kMaximumRetryMs : QueuePolicy::nextRetry(delayMs);
  return millis() + delayMs + (esp_random() % 500);
}

void networkTask(void*) {
  const bool cloudConfigured = configured();
  if (!cloudConfigured) cloudState(CloudState::NeedConfig);
  else {
    WiFi.persistent(false);
    WiFi.mode(WIFI_STA);
    WiFi.setAutoReconnect(true);
    WiFi.begin(DeviceConfig::WIFI_SSID, DeviceConfig::WIFI_PASSWORD);
  }
  uint32_t nextWiFi = millis() + WIFI_RETRY_MS;
  uint32_t nextClock = 0;
  uint32_t nextTelemetry = 0;
  uint32_t nextScan = 0;
  uint32_t scanRetry = 0;
  uint32_t telemetryRetry = 0;
  uint32_t nextLegacyAttempt = 0;
  bool networkReady = false;
  for (;;) {
    uint32_t now = millis();
    if (QueuePolicy::due(now, nextLegacyAttempt)) {
      // At most one old record is copied per worker iteration. Full/unavailable
      // storage leaves its NVS slot intact, and copying resumes after draining.
      const auto migrated = legacyScanMigration.step(scanQueue);
      const bool wait = migrated == LegacyScanMigration::Result::StorageError ||
                        migrated == LegacyScanMigration::Result::DestinationFull;
      nextLegacyAttempt = now + (wait ? 5000 : 100);
      portENTER_CRITICAL(&stateMutex);
      shared.legacyPending = legacyScanMigration.remaining();
      shared.legacyAttention = legacyScanMigration.needsAttention();
      portEXIT_CRITICAL(&stateMutex);
    }
    if (!cloudConfigured) { vTaskDelay(pdMS_TO_TICKS(250)); continue; }
    if (WiFi.status() != WL_CONNECTED) {
      networkReady = false;
      cloudState(CloudState::Connecting);
      if (QueuePolicy::due(now, nextWiFi)) {
        WiFi.disconnect();
        WiFi.begin(DeviceConfig::WIFI_SSID, DeviceConfig::WIFI_PASSWORD);
        nextWiFi = now + WIFI_RETRY_MS;
      }
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    // Refresh inactive deadlines while connected. Otherwise a disconnect after
    // more than 2^31 milliseconds could make the rollover comparison ambiguous.
    nextWiFi = now + WIFI_RETRY_MS;
    if (time(nullptr) < 1704067200) { // A valid current clock is required for TLS.
      networkReady = false;
      cloudState(CloudState::Clock);
      if (QueuePolicy::due(now, nextClock)) {
        configTime(0, 0, "pool.ntp.org", "time.google.com");
        nextClock = now + 30000;
      }
      vTaskDelay(pdMS_TO_TICKS(250));
      continue;
    }
    if (!networkReady) {
      // Also discard inactive deadlines after a long offline/NTP outage. A
      // populated FIFO must start draining immediately when connectivity returns.
      nextTelemetry = now;
      nextScan = now;
      scanRetry = 0;
      telemetryRetry = 0;
      networkReady = true;
    }
    // Telemetry/heartbeat has its own schedule: an invalid queued scan must not
    // prevent sensor updates or online/offline detection.
    if (QueuePolicy::due(now, nextTelemetry)) {
      const UploadResult result = upload(telemetryPayload());
      if (result.acknowledged) {
        telemetryRetry = 0;
        nextTelemetry = millis() + UPLOAD_INTERVAL_MS;
        cloudState(CloudState::Online, result.code);
      } else {
        nextTelemetry = retryDeadline(telemetryRetry, result.code);
        cloudState(QueuePolicy::configurationRejected(result.code) ? CloudState::Rejected : CloudState::Retry, result.code);
      }
    }
    now = millis();
    if (!scanQueue.pending()) {
      // The device may be idle for weeks before its next scan. Do not leave an
      // old deadline outside the signed-comparison window while the FIFO is empty.
      nextScan = now;
      scanRetry = 0;
    }
    if (scanQueue.pending() && QueuePolicy::due(now, nextScan)) {
      String path, payload;
      const auto peeked = scanQueue.peek(path, payload);
      if (peeked == PersistentScanQueue::PeekResult::Ready) {
        const UploadResult result = upload(payload);
        if (result.acknowledged) {
          if (scanQueue.acknowledge(path)) {
            Serial.printf("Scan acknowledged; %u pending.\n", scanQueue.pending());
            scanRetry = 0;
            nextScan = millis() + 100;
          } else {
            Serial.println("Acknowledged scan could not be removed; a backed-off retry will be deduplicated.");
            nextScan = retryDeadline(scanRetry, -1);
          }
          cloudState(CloudState::Online, result.code);
        } else {
          nextScan = retryDeadline(scanRetry, result.code);
          cloudState(QueuePolicy::configurationRejected(result.code) ? CloudState::Rejected : CloudState::Retry, result.code);
        }
      } else {
        nextScan = millis() + 1000;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }
}

void provisionQueueIfRequested() {
  if (LittleFS.begin(false)) { LittleFS.end(); return; }
  Serial.println("Queue storage is not mounted. It will NOT be erased automatically.");
  Serial.println("On a NEW EMPTY device only, type FORMAT_QUEUE within 30 seconds.");
  Serial.println("WARNING: this explicit command formats the data partition and destroys any old queue.");
  lcdLine(0, "Queue not ready");
  lcdLine(1, "See Serial setup");
  const uint32_t deadline = millis() + 30000;
  String line;
  while (!QueuePolicy::due(millis(), deadline)) {
    while (Serial.available()) {
      const char c = Serial.read();
      if (c == '\n') {
        line.trim();
        if (line == "FORMAT_QUEUE") {
          Serial.println(LittleFS.format() ? "Empty queue initialized." : "Queue initialization failed.");
          return;
        }
        line = "";
      } else if (line.length() < 64) line += c;
    }
    delay(10);
  }
}

void setup() {
  Serial.begin(115200);
  Wire.begin(21, 22);
  lcd.init();
  lcd.backlight();
  lcdLine(0, "RFID + DHT22");
  lcdLine(1, "Starting...");
  SPI.begin(18, 19, 23, SS_PIN);
  rfid.PCD_Init();
  dht.begin();
  provisionQueueIfRequested();
  if (!scanQueue.begin()) Serial.println("Queue needs attention. Existing data preserved; scan failures will be displayed.");
  legacyScanMigration.begin();
  shared.legacyPending = legacyScanMigration.remaining();
  shared.legacyAttention = legacyScanMigration.needsAttention();
  if (xTaskCreate(networkTask, "supabase", 12288, nullptr, 1, nullptr) != pdPASS) {
    Serial.println("Could not start network worker. Local sensors still run and scans remain queued.");
    cloudState(CloudState::Retry);
  }
  lcd.clear();
  Serial.println("Storeroom monitor started. Secrets are never printed.");
}

String readCardUID() {
  String uid;
  for (byte i = 0; i < rfid.uid.size; ++i) {
    if (rfid.uid.uidByte[i] < 0x10) uid += '0';
    uid += String(rfid.uid.uidByte[i], HEX);
  }
  uid.toUpperCase();
  return uid;
}

void handleCard(const String& uid) {
  const bool authorized = uid == AUTHORISED_UID;
  const bool sendTag = !DeviceConfig::SEND_ONLY_AUTHORIZED_TAGS || authorized;
  String secondLine = String("UID:") + uid.substring(0, 12);
  if (sendTag) {
    const auto result = scanQueue.append(scanPayload(uid));
    if (result == PersistentScanQueue::AppendResult::Saved) {
      Serial.printf("RFID scan saved locally; %u pending.\n", scanQueue.pending());
    } else if (result == PersistentScanQueue::AppendResult::Full) {
      secondLine = "QUEUE FULL!";
      Serial.println("Queue full. This scan was not saved; wait for upload then scan again.");
    } else {
      secondLine = "STORAGE ERROR!";
      Serial.println("Could not confirm durable scan storage. Inspect queue before rescanning.");
    }
  }
  Serial.print("Card UID: "); Serial.println(uid);
  lcdLine(0, authorized ? "ACCESS GRANTED" : "ACCESS DENIED");
  lcdLine(1, secondLine);
  showingAccessMessage = true;
  accessMessageUntil = millis() + 2500;
}

void checkRFID(uint32_t now) {
  if (now - lastCardProbe < 80) return;
  lastCardProbe = now;
  byte response[2];
  byte size = sizeof(response);
  // Wake halted tags as well. IsNewCardPresent alone cannot establish that a
  // previous card was physically removed and can accidentally re-count it.
  const auto result = rfid.PICC_WakeupA(response, &size);
  const bool present = result == MFRC522::STATUS_OK || result == MFRC522::STATUS_COLLISION;
  const bool mayScan = cardGate.observe(present, now);
  if (!present) return;
  if (!rfid.PICC_ReadCardSerial()) return;
  const String uid = readCardUID();
  if (mayScan) {
    handleCard(uid);
    cardGate.consumed();
    bootRemovalMessageShown = true;
  } else if (!bootRemovalMessageShown) {
    lcdLine(0, "Remove card");
    lcdLine(1, "Then scan again");
    Serial.println("Card present before reader was armed; remove all cards for 350 ms before scanning.");
    showingAccessMessage = true;
    accessMessageUntil = millis() + 2500;
    bootRemovalMessageShown = true;
  }
  rfid.PICC_HaltA();
  rfid.PCD_StopCrypto1();
}

void loop() {
  const uint32_t now = millis();
  if (now - lastSensorRead >= SENSOR_INTERVAL_MS) {
    lastSensorRead = now;
    const float humidity = dht.readHumidity();
    const float temperature = dht.readTemperature();
    const bool ok = isfinite(temperature) && isfinite(humidity) && humidity >= 0 && humidity <= 100;
    portENTER_CRITICAL(&stateMutex);
    shared.temperature = temperature;
    shared.humidity = humidity;
    shared.sensorOK = ok;
    portEXIT_CRITICAL(&stateMutex);
    if (ok) Serial.printf("Temperature: %.1f C | Humidity: %.1f %%\n", temperature, humidity);
    else Serial.println("DHT22 read failed. Check wiring.");
  }
  checkRFID(now);
  if (showingAccessMessage && QueuePolicy::due(now, accessMessageUntil)) {
    showingAccessMessage = false;
    lastLCDUpdate = 0;
  }
  if (!showingAccessMessage && now - lastLCDUpdate >= 1000) {
    lastLCDUpdate = now;
    const SharedState state = snapshot();
    if (state.sensorOK) {
      lcdLine(0, String("Temp: ") + String(state.temperature, 1) + char(223) + "C");
      lcdLine(1, String("Humidity: ") + String(state.humidity, 1) + "%");
    } else {
      lcdLine(0, "DHT22 error");
      lcdLine(1, "Check sensor");
    }
    // Alternate the second line with connection/queue information, without
    // hiding the original climate display or interrupting an access message.
    if ((now / 3000) % 2) {
      if (scanQueue.needsAttention()) lcdLine(1, "QUEUE NEEDS HELP");
      else if (state.legacyAttention) lcdLine(1, "OLD QUEUE ERROR");
      else if (state.cloud == CloudState::NeedConfig) lcdLine(1, "SET secrets.h");
      else if (state.cloud == CloudState::Connecting) lcdLine(1, "WiFi reconnect");
      else if (state.cloud == CloudState::Clock) lcdLine(1, "Syncing clock");
      else if (state.cloud == CloudState::Rejected) lcdLine(1, String("Cloud error ") + state.httpCode);
      else if (state.legacyPending) lcdLine(1, String("Old scans: ") + state.legacyPending);
      else if (scanQueue.pending()) lcdLine(1, String("Queued scans: ") + scanQueue.pending());
      else if (state.cloud == CloudState::Retry) lcdLine(1, "Cloud retrying");
      else lcdLine(1, "Cloud connected");
    }
  }
  delay(5);
}
