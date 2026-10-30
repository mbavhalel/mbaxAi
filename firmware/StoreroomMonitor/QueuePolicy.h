#pragma once

#include <stdint.h>

// Small policy helpers are independent of Arduino so they can be unit-tested.
namespace QueuePolicy {
constexpr uint16_t kCapacity = 128;
constexpr uint32_t kInitialRetryMs = 1000;
constexpr uint32_t kMaximumRetryMs = 60000;

inline bool due(uint32_t now, uint32_t deadline) {
  return static_cast<int32_t>(now - deadline) >= 0;
}

inline uint32_t nextRetry(uint32_t previous) {
  if (previous < kInitialRetryMs) return kInitialRetryMs;
  if (previous >= kMaximumRetryMs / 2) return kMaximumRetryMs;
  return previous * 2;
}

inline bool configurationRejected(int code) {
  return code >= 400 && code < 500 && code != 408 && code != 425 && code != 429;
}
}  // namespace QueuePolicy
