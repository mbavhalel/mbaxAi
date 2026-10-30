"""Native tests for the same card/retry policies used by the ESP32 sketch."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FirmwarePolicyTests(unittest.TestCase):
    def test_serial_queue_setup_accepts_complete_commands_without_a_deadline(self):
        compiler = shutil.which("g++")
        if not compiler:
            self.skipTest("g++ is required for native firmware policy tests")
        code = r'''
          #include <cassert>
          #include <cstdint>
          #include <string>
          #include "SerialSetupCommand.h"
          using Command = SerialSetupCommand::Command;
          void burst(SerialSetupCommand& parser, const std::string& text, uint32_t now) {
            for (char value : text) assert(parser.feed(value,now) == Command::None);
          }
          int main() {
            SerialSetupCommand parser;
            const uint32_t late = 3000000000U; // Setup still works long after boot.
            burst(parser,"FORMAT_QUEUE",late);
            assert(parser.poll(late+199) == Command::None);
            assert(parser.poll(late+200) == Command::FormatQueue);
            assert(parser.poll(late+1000) == Command::None);
            burst(parser,"FoRmAt_QuEuE",late+2000);
            assert(parser.feed('\r',late+2000) == Command::FormatQueue);
            assert(parser.feed('\n',late+2000) == Command::None);
            burst(parser,"  retry_queue\t",late+3000);
            assert(parser.feed('\n',late+3000) == Command::RetryQueue);
            // A valid prefix must never execute a malformed formatting command.
            const std::string invalid[] = {"FORMAT_QUEUE_EXTRA","junkFORMAT_QUEUE",std::string(100,'X')+"FORMAT_QUEUE",
              std::string("FORMAT_QUEUE\0EXTRA",18),std::string("FORMAT_QUEUE")+char(0xff)};
            for (const std::string& text : invalid) {
              burst(parser,text,100);
              assert(parser.poll(300) == Command::None);
            }
            burst(parser,"FORMAT_QUEUE",UINT32_MAX-50);
            assert(parser.poll(148) == Command::None);
            assert(parser.poll(149) == Command::FormatQueue);
            burst(parser,"FORMAT_QUEUE",1000);
            parser.reset();
            assert(parser.poll(2000) == Command::None);
          }
        '''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "serial.cpp"
            binary = Path(directory) / "serial"
            source.write_text(code)
            compiled = subprocess.run([compiler,"-std=c++17","-Wall","-Wextra","-Werror",
                "-I",str(ROOT / "firmware/StoreroomMonitor"),str(source),"-o",str(binary)],
                text=True,capture_output=True)
            self.assertEqual(compiled.returncode,0,compiled.stderr)
            executed = subprocess.run([str(binary)],text=True,capture_output=True)
            self.assertEqual(executed.returncode,0,executed.stderr)

    def test_card_presentations_and_retry_timers(self):
        compiler = shutil.which("g++")
        if not compiler:
            self.skipTest("g++ is required for native firmware policy tests")
        code = r'''
          #include <cassert>
          #include <cstdint>
          #include <cstring>
          #include "CardGate.h"
          #include "QueuePolicy.h"
          #include "LegacyScanPolicy.h"
          int main() {
            CardGate gate;
            // A held tag at boot is never a fresh scan, even after a long wait.
            assert(!gate.observe(true, 0));
            assert(!gate.observe(true, 10000));
            assert(!gate.observe(false, 10001));
            assert(!gate.observe(false, 10350));
            assert(!gate.armed());
            gate.observe(false, 10351);
            assert(gate.observe(true, 10400));
            gate.consumed();
            assert(!gate.observe(true, 10500));
            // Brief RF gaps/bounce must not count a held card again.
            gate.observe(false, 10600);
            assert(!gate.observe(true, 10800));
            gate.observe(false, 10900);
            assert(!gate.observe(true, 11100));
            gate.observe(false, 11200);
            gate.observe(false, 11550);
            assert(gate.observe(true, 11600));
            gate.consumed();
            assert(!gate.observe(true, 11700));
            // Card removal and network retry deadlines survive millis rollover.
            CardGate rollover;
            rollover.observe(false, UINT32_MAX - 100);
            rollover.observe(false, 249);
            assert(rollover.observe(true, 250));
            assert(!QueuePolicy::due(UINT32_MAX - 10, 5));
            assert(QueuePolicy::due(5, 5));
            assert(QueuePolicy::due(7, 5));
            assert(QueuePolicy::due(0, UINT32_MAX - 9));
            uint32_t delay = 0;
            const uint32_t expected[] = {1000,2000,4000,8000,16000,32000,60000,60000};
            for (auto value : expected) { delay = QueuePolicy::nextRetry(delay); assert(delay == value); }
            for (int code : {-1, 408, 425, 429, 500, 503}) assert(!QueuePolicy::configurationRejected(code));
            for (int code : {400, 401, 403, 404, 413, 415}) assert(QueuePolicy::configurationRejected(code));
            char uid[21];
            assert(LegacyScanPolicy::normalizeUid("12:7C:12:07", uid, sizeof(uid)));
            assert(std::strcmp(uid, "127C1207") == 0);
            assert(!LegacyScanPolicy::normalizeUid("<script>", uid, sizeof(uid)));
            assert(!LegacyScanPolicy::normalizeUid("12:7C:12", uid, sizeof(uid)));
            const char* original = "00112233-4455-4677-8899-aabbccddeeff";
            assert(LegacyScanPolicy::validEventId(original));
            assert(!LegacyScanPolicy::validEventId("not-a-uuid"));
          }
        '''
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "policy.cpp"
            binary = Path(directory) / "policy"
            source.write_text("#include <initializer_list>\n" + code)
            compiled = subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                "-I", str(ROOT / "firmware/StoreroomMonitor"), str(source), "-o", str(binary)],
                text=True, capture_output=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            executed = subprocess.run([str(binary)], text=True, capture_output=True)
            self.assertEqual(executed.returncode, 0, executed.stderr)


if __name__ == "__main__":
    unittest.main()
