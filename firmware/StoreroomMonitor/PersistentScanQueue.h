#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include "QueuePolicy.h"

// Single append/delete operations are serialized, but this mutex is NEVER held
// while the networking task performs HTTPS. Files survive reboots and reflashes
// that preserve the data partition. A queue is never automatically formatted.
class PersistentScanQueue {
 public:
  enum class AppendResult { Saved, Full, StorageError };
  enum class PeekResult { Empty, Ready, StorageError, Quarantined };

  bool begin() {
    lock_ = xSemaphoreCreateMutex();
    if (!lock_ || !LittleFS.begin(false) || !preferences_.begin("storeroom", false)) {
      healthy_ = false;
      return false;
    }
    mounted_ = true;
    sequence_ = preferences_.getULong64("sequence", 0);
    recoverTemporaryFiles();
    recount();
    return healthy_;
  }

  AppendResult append(const String& payload) {
    Guard guard(lock_);
    if (!mounted_ || !guard.locked()) return AppendResult::StorageError;
    if (count_ + quarantined_ >= QueuePolicy::kCapacity) return AppendResult::Full;
    if (!validPayload(payload)) return AppendResult::StorageError;
    if (sequence_ == UINT64_MAX) return AppendResult::StorageError;
    const uint64_t sequence = sequence_ + 1;
    // Consume a sequence before the file write. A power failure may leave a gap,
    // but cannot make a later scan overwrite or overtake an existing scan.
    if (preferences_.putULong64("sequence", sequence) != sizeof(sequence)) {
      healthy_ = false;
      return AppendResult::StorageError;
    }
    sequence_ = sequence;
    const String path = filename(sequence, ".json");
    const String temporary = filename(sequence, ".tmp");
    File file = LittleFS.open(temporary, FILE_WRITE);
    if (!file) { healthy_ = false; return AppendResult::StorageError; }
    const size_t written = file.print(payload);
    file.flush();
    file.close();
    if (written != payload.length() || !LittleFS.rename(temporary, path)) {
      healthy_ = false;
      if (LittleFS.exists(temporary)) ++quarantined_;
      return AppendResult::StorageError;
    }
    ++count_;
    return AppendResult::Saved;
  }

  PeekResult peek(String& path, String& payload) {
    Guard guard(lock_);
    if (!mounted_ || !guard.locked()) return PeekResult::StorageError;
    path = "";
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; return PeekResult::StorageError; }
    for (File file = root.openNextFile(); file; file = root.openNextFile()) {
      const String name = rootPath(file.name());
      if (!file.isDirectory() && isQueueFile(name, ".json") && (!path.length() || name < path)) path = name;
      file.close();
    }
    root.close();
    if (!path.length()) { count_ = 0; return PeekResult::Empty; }
    File file = LittleFS.open(path, FILE_READ);
    if (!file) { healthy_ = false; return PeekResult::StorageError; }
    const size_t size = file.size();
    payload = size <= 256 ? file.readString() : String();
    file.close();
    if (!validPayload(payload)) {
      String quarantine = path.substring(0, path.length() - 5) + ".bad";
      if (LittleFS.exists(quarantine) || !LittleFS.rename(path, quarantine)) {
        healthy_ = false;
        return PeekResult::StorageError;
      }
      if (count_) --count_;
      ++quarantined_;
      healthy_ = false;
      Serial.println("A corrupt scan was preserved as .bad; inspect queue storage before clearing it.");
      return PeekResult::Quarantined;
    }
    return PeekResult::Ready;
  }

  bool acknowledge(const String& path) {
    Guard guard(lock_);
    if (!mounted_ || !guard.locked() || !isQueueFile(path, ".json") || !LittleFS.remove(path)) {
      healthy_ = false;
      return false;
    }
    if (count_) --count_;
    return true;
  }

  uint16_t pending() {
    Guard guard(lock_);
    if (!guard.locked()) return 0;
    return count_;
  }

  bool needsAttention() {
    Guard guard(lock_);
    if (!guard.locked()) return true;
    return !healthy_ || quarantined_ > 0;
  }

 private:
  class Guard {
   public:
    explicit Guard(SemaphoreHandle_t lock) : lock_(lock), acquired_(lock && xSemaphoreTake(lock, pdMS_TO_TICKS(2000)) == pdTRUE) {}
    ~Guard() { if (acquired_) xSemaphoreGive(lock_); }
    bool locked() const { return acquired_; }
   private:
    SemaphoreHandle_t lock_;
    bool acquired_;
  };

  SemaphoreHandle_t lock_ = nullptr;
  Preferences preferences_;
  uint64_t sequence_ = 0;
  uint16_t count_ = 0;
  uint16_t quarantined_ = 0;
  bool mounted_ = false;
  bool healthy_ = true;

  static String rootPath(const char* name) {
    return name[0] == '/' ? String(name) : String("/") + name;
  }

  static bool isQueueFile(const String& name, const char* suffix) {
    if (!name.startsWith("/scan-") || !name.endsWith(suffix)) return false;
    if (name.length() != 6 + 20 + strlen(suffix)) return false;
    for (size_t i = 6; i < 26; ++i) if (name[i] < '0' || name[i] > '9') return false;
    return true;
  }

  static String filename(uint64_t sequence, const char* suffix) {
    char number[21];
    snprintf(number, sizeof(number), "%020llu", static_cast<unsigned long long>(sequence));
    return String("/scan-") + number + suffix;
  }

  static bool validPayload(const String& payload) {
    if (!payload.length() || payload.length() > 256) return false;
    JsonDocument document;
    if (deserializeJson(document, payload) || document["type"] != "scan") return false;
    const char* uid = document["uid"] | "";
    const char* id = document["event_id"] | "";
    const size_t length = strlen(uid);
    if (length != 8 && length != 14 && length != 20) return false;
    for (size_t i = 0; i < length; ++i) if (!isxdigit(static_cast<unsigned char>(uid[i]))) return false;
    if (strlen(id) != 36) return false;
    for (size_t i = 0; i < 36; ++i) {
      const bool separator = i == 8 || i == 13 || i == 18 || i == 23;
      if (separator ? id[i] != '-' : !isxdigit(static_cast<unsigned char>(id[i]))) return false;
    }
    return true;
  }

  void recoverTemporaryFiles() {
    // This runs before the worker task exists. A complete temporary file can be
    // recovered after a reset between flush and rename; a partial file is kept.
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; return; }
    String temporary[QueuePolicy::kCapacity];
    size_t number = 0;
    for (File file = root.openNextFile(); file; file = root.openNextFile()) {
      const String name = rootPath(file.name());
      if (isQueueFile(name, ".tmp") && number < QueuePolicy::kCapacity) temporary[number++] = name;
      file.close();
    }
    root.close();
    for (size_t i = 0; i < number; ++i) {
      File file = LittleFS.open(temporary[i], FILE_READ);
      String payload = file && file.size() <= 256 ? file.readString() : String();
      file.close();
      const String base = temporary[i].substring(0, temporary[i].length() - 4);
      const String destination = base + (validPayload(payload) ? ".json" : ".bad");
      if (LittleFS.exists(destination) || !LittleFS.rename(temporary[i], destination)) healthy_ = false;
      if (destination.endsWith(".bad")) healthy_ = false;
    }
  }

  void recount() {
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; return; }
    for (File file = root.openNextFile(); file; file = root.openNextFile()) {
      const String name = rootPath(file.name());
      if (isQueueFile(name, ".json")) ++count_;
      if (isQueueFile(name, ".bad") || isQueueFile(name, ".tmp")) { ++quarantined_; healthy_ = false; }
      if (name.startsWith("/scan-") && name.length() >= 26) {
        const uint64_t fromFile = strtoull(name.substring(6, 26).c_str(), nullptr, 10);
        if (fromFile > sequence_) sequence_ = fromFile;
      }
      file.close();
    }
    root.close();
  }
};
