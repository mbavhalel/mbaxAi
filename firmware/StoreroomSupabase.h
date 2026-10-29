#pragma once

// ESP32 Arduino helper; uses only the ESP32 core libraries.
// The existing sensor/RFID firmware is not part of this repository.
#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <esp_random.h>
#include <ctype.h>
#include <math.h>

class StoreroomSupabase {
 public:
  StoreroomSupabase(const char* endpoint, const char* deviceToken, const char* rootCa)
      : endpoint_(endpoint), token_(deviceToken), rootCa_(rootCa) {}

  static String eventId() {
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

  // Generate this ONCE for each physical scan. Keep the entire returned payload
  // if sending fails; every retry must reuse its original event_id.
  static String scan(const String& uid) {
    String compact;
    for (size_t i = 0; i < uid.length(); ++i) {
      const char c = uid[i];
      if (isxdigit(static_cast<unsigned char>(c))) compact += c;
      else if (c != ':' && c != ' ' && c != '-' && c != '_') return String();
    }
    if (compact.length() != 8 && compact.length() != 14 && compact.length() != 20) return String();
    compact.toUpperCase();
    return String("{\"type\":\"scan\",\"event_id\":\"") + eventId() + "\",\"uid\":\"" + compact + "\"}";
  }

  static String climate(float temperature, float humidity) {
    if (!isfinite(temperature) || !isfinite(humidity) || humidity < 0 || humidity > 100) return String();
    return String("{\"type\":\"climate\",\"t\":") + String(temperature, 2) + ",\"h\":" + String(humidity, 2) + "}";
  }

  // A 2xx response means stored (including a safely deduplicated retry).
  // Network errors, 429 and 5xx can be retried with this SAME payload.
  // 400/401/403 require fixing the data/token/configuration first.
  int send(const String& payload) const {
    if (!payload.length() || !rootCa_ || !rootCa_[0]) return -1;
    WiFiClientSecure tls;
    tls.setCACert(rootCa_);  // Never call setInsecure(). Sync the clock before use.
    HTTPClient http;
    http.setTimeout(60000); // Allows optional Telegram delivery after storage.
    if (!http.begin(tls, endpoint_)) return -1;
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Token", token_);
    int code = http.POST(payload);
    http.end();
    return code;
  }

 private:
  const char* endpoint_;
  const char* token_;
  const char* rootCa_;
};
