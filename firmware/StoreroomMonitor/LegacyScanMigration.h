#pragma once

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>
#include "LegacyScanPolicy.h"
#include "PersistentScanQueue.h"

// Upgrade the user's existing NVS queue, mbaxscan/q0 ... q31. The previous
// consumer used ascending slot order, not a head/tail ring, so do the same here.
// No clear() or namespace erase is performed. LittleFS is the durable hand-off.
class LegacyScanMigration {
 public:
  enum class Result { Idle, Copied, InvalidPreserved, DestinationFull, StorageError };

  void begin() {
    // A new installation need not create the old namespace. First check for it
    // read-only, and reopen writable only if it already exists.
    Preferences existing;
    if (!existing.begin("mbaxscan", true)) return;
    existing.end();
    if (!legacy_.begin("mbaxscan", false)) { attention_ = true; return; }
    opened_ = true;
    for (uint8_t slot = 0; slot < kSlots; ++slot) if (legacy_.isKey(key(slot).c_str())) ++remaining_;
    if (remaining_) Serial.printf("Found %u legacy saved scans; migrating without erasing the old namespace.\n", remaining_);
  }

  Result step(PersistentScanQueue& destination) {
    if (!opened_ || !remaining_) return Result::Idle;
    // If NVS deletion failed after a successful copy, retry deletion without
    // repeatedly filling LittleFS with copies. After a reset, one extra copy is
    // harmless: the stored payload still has its ORIGINAL UUID.
    if (awaitingRemoval_ >= 0) return finishRemoval();
    for (uint8_t slot = 0; slot < kSlots; ++slot) {
      if (invalidSlots_ & (uint32_t(1) << slot)) continue;
      const String name = key(slot);
      if (!legacy_.isKey(name.c_str())) continue;
      const size_t size = legacy_.getStringLength(name.c_str());
      const String oldPayload = size <= 1025 ? legacy_.getString(name.c_str(), "") : String();
      String payload;
      if (!normalizePayload(oldPayload, payload)) {
        invalidSlots_ |= uint32_t(1) << slot;
        attention_ = true;
        Serial.printf("Legacy scan slot q%u is invalid and was PRESERVED in NVS; inspect it before clearing anything.\n", slot);
        return Result::InvalidPreserved;
      }
      const auto copied = destination.append(payload);
      if (copied == PersistentScanQueue::AppendResult::Full) return Result::DestinationFull;
      if (copied != PersistentScanQueue::AppendResult::Saved) {
        attention_ = true;
        return Result::StorageError;
      }
      // A power loss here can make two queue files containing the same original
      // event_id. Supabase's transaction safely counts that scan only once.
      awaitingRemoval_ = slot;
      return finishRemoval();
    }
    return Result::Idle;
  }

  uint8_t remaining() const { return remaining_; }
  bool needsAttention() const { return attention_; }

 private:
  static constexpr uint8_t kSlots = 32;
  Preferences legacy_;
  bool opened_ = false;
  bool attention_ = false;
  uint8_t remaining_ = 0;
  uint32_t invalidSlots_ = 0;
  int awaitingRemoval_ = -1;

  static String key(uint8_t slot) { return String("q") + slot; }

  Result finishRemoval() {
    const String name = key(static_cast<uint8_t>(awaitingRemoval_));
    if (!legacy_.remove(name.c_str())) {
      attention_ = true;
      return Result::StorageError;
    }
    awaitingRemoval_ = -1;
    if (remaining_) --remaining_;
    if (!remaining_) attention_ = false;
    Serial.printf("Legacy scan copied durably; %u old slots remain.\n", remaining_);
    return Result::Copied;
  }

  static bool normalizePayload(const String& input, String& output) {
    if (!input.length()) return false;
    JsonDocument old;
    if (deserializeJson(old, input) || old["type"] != "scan" || !old["uid"].is<const char*>() || !old["event_id"].is<const char*>()) return false;
    const char* id = old["event_id"];
    char compact[21];
    if (!LegacyScanPolicy::validEventId(id) || !LegacyScanPolicy::normalizeUid(old["uid"].as<const char*>(), compact, sizeof(compact))) return false;
    JsonDocument normalized;
    normalized["type"] = "scan";
    normalized["uid"] = compact;
    normalized["event_id"] = id;  // Never generate a new ID for an old scan.
    serializeJson(normalized, output);
    return output.length() <= 256;
  }
};
