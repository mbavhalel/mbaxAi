#pragma once

#include <Arduino.h>

// HTTPClient::writeToStream decodes chunked responses for this sink. Keeping a
// strict bound also handles unknown Content-Length without growing heap strings.
class BoundedResponse : public Stream {
 public:
  static constexpr size_t kCapacity = 2048;
  size_t write(uint8_t value) override { return write(&value, 1); }
  size_t write(const uint8_t* data, size_t length) override {
    if (length > kCapacity - length_) { overflowed_ = true; return 0; }
    memcpy(bytes_ + length_, data, length);
    length_ += length;
    bytes_[length_] = '\0';
    return length;
  }
  int available() override { return 0; }
  int read() override { return -1; }
  int peek() override { return -1; }
  void flush() override {}
  bool overflowed() const { return overflowed_; }
  const char* data() const { return bytes_; }
  size_t size() const { return length_; }

 private:
  char bytes_[kCapacity + 1] = {};
  size_t length_ = 0;
  bool overflowed_ = false;
};
