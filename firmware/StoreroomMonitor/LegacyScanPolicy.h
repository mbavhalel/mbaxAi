#pragma once

#include <stddef.h>
#include <string.h>
#include <ctype.h>

// The previous firmware stored colon-separated RFID IDs. Preserve its UUID and
// only normalize the tag representation when moving a record to the new queue.
namespace LegacyScanPolicy {
inline bool normalizeUid(const char* input, char* output, size_t capacity) {
  if (!input || !output || capacity < 21) return false;
  size_t length = 0;
  for (size_t i = 0; input[i]; ++i) {
    const unsigned char value = static_cast<unsigned char>(input[i]);
    if (isxdigit(value)) {
      if (length >= 20) return false;
      output[length++] = static_cast<char>(toupper(value));
    } else if (value != ':' && value != '_' && value != '-' && !isspace(value)) {
      return false;
    }
  }
  output[length] = '\0';
  return length == 8 || length == 14 || length == 20;
}

inline bool validEventId(const char* value) {
  if (!value || strlen(value) != 36) return false;
  for (size_t i = 0; i < 36; ++i) {
    const bool separator = i == 8 || i == 13 || i == 18 || i == 23;
    if (separator ? value[i] != '-' : !isxdigit(static_cast<unsigned char>(value[i]))) return false;
  }
  return true;
}
}  // namespace LegacyScanPolicy
