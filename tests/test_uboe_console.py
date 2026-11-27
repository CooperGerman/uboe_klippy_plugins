import unittest
from unittest import mock

from uboe_console import console_log


class ConsoleLogTests(unittest.TestCase):
    def test_success_escapes_text_and_logs_multiline_message(self):
        gcode = mock.Mock()
        with self.assertLogs(level="INFO") as logs:
            console_log(gcode, "Done <ok>\nagain", "one & two\nnext", "success")
        rendered = gcode.respond_raw.call_args.args[0]
        self.assertIn("#a3e635", rendered)
        self.assertIn("Done &lt;ok&gt;<br>again", rendered)
        self.assertIn("one &amp; two<br>next", rendered)
        self.assertIn("Done <ok>\nagain: one & two", logs.output[0])

    def test_warning_uses_warning_log_level(self):
        gcode = mock.Mock()
        with self.assertLogs(level="WARNING") as logs:
            console_log(gcode, "Warning", "Careful", "warning")
        self.assertIn("WARNING:root:Warning: Careful", logs.output[0])
        self.assertIn("#fbbf24", gcode.respond_raw.call_args.args[0])

    def test_invalid_severity_fails_without_emitting(self):
        gcode = mock.Mock()
        with self.assertRaisesRegex(ValueError, "Unknown console severity"):
            console_log(gcode, "Title", "Message", "unknown")
        gcode.respond_raw.assert_not_called()


if __name__ == "__main__":
    unittest.main()
