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
    if (!lock_) lock_ = xSemaphoreCreateMutex();
    if (!lock_) {
      healthy_ = false;
      Serial.println("Queue setup failed: could not create storage mutex.");
      return false;
    }
    return retryQueue();
  }

  // This is deliberately refused for EVERY mounted filesystem, including one
  // whose NVS sequence metadata could not be opened. An NVS failure must never
  // be mistaken for an empty/unformatted filesystem.
  bool initializeEmptyQueue() {
    Guard guard(lock_);
    if (!guard.locked()) { Serial.println("Queue format refused: storage mutex unavailable; retry shortly."); return false; }
    if (filesystemMounted_) {
      Serial.println("Queue format REFUSED: filesystem is already mounted; existing scan data is protected. Use RETRY_QUEUE for metadata errors.");
      return false;
    }
    Serial.println("Explicit FORMAT_QUEUE requested: formatting LittleFS only. Legacy NVS scans are not erased.");
    if (!LittleFS.format()) {
      healthy_ = false; error_ = Error::Format;
      Serial.println("Queue setup failed: LittleFS format failed; no queue is ready.");
      return false;
    }
    if (!LittleFS.begin(false)) {
      healthy_ = false; error_ = Error::Mount;
      Serial.println("Queue setup failed: format completed but LittleFS could not mount.");
      return false;
    }
    filesystemMounted_ = true;
    if (!openAndLoadLocked()) return false;
    Serial.println("Empty queue initialized and mounted. Scans can now be saved.");
    return true;
  }

  // Retry mounting or opening metadata without formatting or clearing anything.
  bool retryQueue() {
    Guard guard(lock_);
    if (!guard.locked()) { Serial.println("Queue retry failed: storage mutex unavailable."); return false; }
    if (!filesystemMounted_) {
      if (!LittleFS.begin(false)) {
        healthy_ = false; error_ = Error::Mount;
        Serial.println("LittleFS is not mounted. Existing data is preserved. For a known empty data partition, send FORMAT_QUEUE in Serial Monitor at any time.");
        return false;
      }
      filesystemMounted_ = true;
    }
    if (!openAndLoadLocked()) return false;
    Serial.printf("Scan queue ready; %u pending.\n", count_);
    return true;
  }

  const char* errorReason() {
    Guard guard(lock_);
    if (!guard.locked()) return "Queue storage mutex is unavailable; retry shortly.";
    if (!filesystemMounted_) return "LittleFS is not mounted. Send FORMAT_QUEUE only if the data partition is known empty.";
    if (!preferencesReady_) return "LittleFS is mounted but NVS metadata is unavailable. Send RETRY_QUEUE; formatting is refused.";
    switch (error_) {
      case Error::None: return "Queue operation failed; inspect prior storage diagnostics.";
      case Error::Mount: return "LittleFS mount failed; existing data was preserved.";
      case Error::Preferences: return "NVS sequence metadata could not be opened or saved.";
      case Error::Format: return "Explicit LittleFS formatting failed.";
      case Error::Invalid: return "Scan payload is invalid and was not queued.";
      case Error::Sequence: return "Queue sequence exhausted; inspect stored records.";
      case Error::Open: return "Could not open a LittleFS queue file or directory.";
      case Error::Write: return "Could not finish writing or renaming a durable scan file.";
      case Error::Remove: return "Could not remove an acknowledged scan file; retries remain idempotent.";
      case Error::Quarantine: return "A corrupt scan file was preserved for inspection.";
    }
    return "Queue storage needs inspection.";
  }

  AppendResult append(const String& payload) {
    Guard guard(lock_);
    if (!guard.locked() || !filesystemMounted_ || !preferencesReady_) return AppendResult::StorageError;
    if (count_ + quarantined_ >= QueuePolicy::kCapacity) return AppendResult::Full;
    if (!validPayload(payload)) { error_ = Error::Invalid; return AppendResult::StorageError; }
    if (sequence_ == UINT64_MAX) { error_ = Error::Sequence; return AppendResult::StorageError; }
    const uint64_t sequence = sequence_ + 1;
    // Consume a sequence before the file write. A power failure may leave a gap,
    // but cannot make a later scan overwrite or overtake an existing scan.
    if (preferences_.putULong64("sequence", sequence) != sizeof(sequence)) {
      healthy_ = false;
      error_ = Error::Preferences;
      return AppendResult::StorageError;
    }
    sequence_ = sequence;
    const String path = filename(sequence, ".json");
    const String temporary = filename(sequence, ".tmp");
    File file = LittleFS.open(temporary, FILE_WRITE);
    if (!file) { healthy_ = false; error_ = Error::Open; return AppendResult::StorageError; }
    const size_t written = file.print(payload);
    file.flush();
    file.close();
    if (written != payload.length() || !LittleFS.rename(temporary, path)) {
      healthy_ = false;
      error_ = Error::Write;
      if (LittleFS.exists(temporary)) ++quarantined_;
      return AppendResult::StorageError;
    }
    ++count_;
    return AppendResult::Saved;
  }

  PeekResult peek(String& path, String& payload) {
    Guard guard(lock_);
    if (!guard.locked() || !filesystemMounted_ || !preferencesReady_) return PeekResult::StorageError;
    path = "";
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; error_ = Error::Open; return PeekResult::StorageError; }
    for (File file = root.openNextFile(); file; file = root.openNextFile()) {
      const String name = rootPath(file.name());
      if (!file.isDirectory() && isQueueFile(name, ".json") && (!path.length() || name < path)) path = name;
      file.close();
    }
    root.close();
    if (!path.length()) { count_ = 0; return PeekResult::Empty; }
    File file = LittleFS.open(path, FILE_READ);
    if (!file) { healthy_ = false; error_ = Error::Open; return PeekResult::StorageError; }
    const size_t size = file.size();
    payload = size <= 256 ? file.readString() : String();
    file.close();
    if (!validPayload(payload)) {
      String quarantine = path.substring(0, path.length() - 5) + ".bad";
      if (LittleFS.exists(quarantine) || !LittleFS.rename(path, quarantine)) {
        healthy_ = false;
        error_ = Error::Write;
        return PeekResult::StorageError;
      }
      if (count_) --count_;
      ++quarantined_;
      healthy_ = false;
      error_ = Error::Quarantine;
      Serial.println("A corrupt scan was preserved as .bad; inspect queue storage before clearing it.");
      return PeekResult::Quarantined;
    }
    return PeekResult::Ready;
  }

  bool acknowledge(const String& path) {
    Guard guard(lock_);
    if (!guard.locked()) return false;
    if (!filesystemMounted_ || !preferencesReady_ || !isQueueFile(path, ".json") || !LittleFS.remove(path)) {
      healthy_ = false;
      error_ = Error::Remove;
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
  enum class Error : uint8_t { None, Mount, Preferences, Format, Invalid, Sequence, Open, Write, Remove, Quarantine };
  bool filesystemMounted_ = false;
  bool preferencesReady_ = false;
  bool healthy_ = true;
  Error error_ = Error::None;

  // Called only with the existing mutex held, both at boot and during runtime
  // setup. Network operations never hold this mutex, so successful setup is
  // immediately visible to appends, acknowledgments, and legacy migration.
  bool openAndLoadLocked() {
    if (!preferencesReady_) {
      if (!preferences_.begin("storeroom", false)) {
        healthy_ = false; error_ = Error::Preferences;
        Serial.println("Queue setup failed: LittleFS mounted, but NVS sequence metadata could not open. Data preserved; send RETRY_QUEUE, not FORMAT_QUEUE.");
        return false;
      }
      preferencesReady_ = true;
    }
    healthy_ = true;
    error_ = Error::None;
    count_ = 0;
    quarantined_ = 0;
    sequence_ = preferences_.getULong64("sequence", 0);
    recoverTemporaryFiles();
    recount();
    if (!healthy_) {
      Serial.println("Queue mounted with stored records needing inspection. Existing files were preserved.");
      return false;
    }
    return true;
  }

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
    // Called with the same storage mutex held, including during runtime retry.
    // A complete file can be recovered after a reset between flush and rename.
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; error_ = Error::Open; return; }
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
      if (LittleFS.exists(destination) || !LittleFS.rename(temporary[i], destination)) { healthy_ = false; error_ = Error::Write; }
      if (destination.endsWith(".bad")) { healthy_ = false; error_ = Error::Quarantine; }
    }
  }

  void recount() {
    File root = LittleFS.open("/");
    if (!root) { healthy_ = false; error_ = Error::Open; return; }
    for (File file = root.openNextFile(); file; file = root.openNextFile()) {
      const String name = rootPath(file.name());
      if (isQueueFile(name, ".json")) ++count_;
      if (isQueueFile(name, ".bad") || isQueueFile(name, ".tmp")) { ++quarantined_; healthy_ = false; error_ = Error::Quarantine; }
      if (name.startsWith("/scan-") && name.length() >= 26) {
        const uint64_t fromFile = strtoull(name.substring(6, 26).c_str(), nullptr, 10);
        if (fromFile > sequence_) sequence_ = fromFile;
      }
      file.close();
    }
    root.close();
  }
};
