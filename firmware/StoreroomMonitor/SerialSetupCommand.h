#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

// Serial Monitor sends one burst when Send is pressed. A line ending executes
// immediately; No line ending executes only after the complete burst is idle.
// Waiting for the burst to finish prevents FORMAT_QUEUE_EXTRA from formatting.
class SerialSetupCommand {
 public:
  enum class Command { None, FormatQueue, RetryQueue };
  static constexpr uint32_t kIdleMs = 200;

  Command feed(char value, uint32_t now) {
    if (value == '\r' || value == '\n') return finish();
    lastInput_ = now;
    pending_ = true;
    const uint8_t byte = static_cast<uint8_t>(value);
    if ((byte < 32 && value != '\t') || byte > 126) {
      overflow_ = true; // Reject embedded NUL/control bytes rather than truncating.
      return Command::None;
    }
    if (length_ >= sizeof(buffer_) - 1) {
      overflow_ = true;
      return Command::None;
    }
    buffer_[length_++] = value >= 'a' && value <= 'z' ? value - 'a' + 'A' : value;
    buffer_[length_] = '\0';
    return Command::None;
  }

  Command poll(uint32_t now) {
    if (pending_ && static_cast<uint32_t>(now - lastInput_) >= kIdleMs) return finish();
    return Command::None;
  }

  void reset() {
    length_ = 0;
    buffer_[0] = '\0';
    pending_ = false;
    overflow_ = false;
  }

 private:
  char buffer_[48] = {};
  size_t length_ = 0;
  uint32_t lastInput_ = 0;
  bool pending_ = false;
  bool overflow_ = false;

  Command finish() {
    Command command = Command::None;
    if (!overflow_) {
      size_t start = 0;
      while (start < length_ && (buffer_[start] == ' ' || buffer_[start] == '\t')) ++start;
      while (length_ > start && (buffer_[length_ - 1] == ' ' || buffer_[length_ - 1] == '\t')) --length_;
      buffer_[length_] = '\0';
      if (strcmp(buffer_ + start, "FORMAT_QUEUE") == 0) command = Command::FormatQueue;
      else if (strcmp(buffer_ + start, "RETRY_QUEUE") == 0) command = Command::RetryQueue;
    }
    reset();
    return command;
  }
};
