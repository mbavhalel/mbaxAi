#pragma once

#include <stdint.h>

// A card already resting on the antenna at boot must not count as a fresh scan.
// After any accepted presentation, require a clear field before the next one.
class CardGate {
 public:
  static constexpr uint32_t kRemovalMs = 350;

  bool observe(bool present, uint32_t now) {
    if (present) {
      absenceStarted_ = false;
      return armed_;
    }
    if (!absenceStarted_) {
      absenceSince_ = now;
      absenceStarted_ = true;
    }
    if (static_cast<uint32_t>(now - absenceSince_) >= kRemovalMs) armed_ = true;
    return false;
  }

  void consumed() {
    armed_ = false;
    absenceStarted_ = false;
  }

  bool armed() const { return armed_; }

 private:
  bool armed_ = false;
  bool absenceStarted_ = false;
  uint32_t absenceSince_ = 0;
};
