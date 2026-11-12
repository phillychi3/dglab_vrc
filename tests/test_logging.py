import logging
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import main
import osc


class LoggingTests(unittest.TestCase):
    def test_app_debug_switch_does_not_enable_dependency_debug(self):
        with patch("main.logging.basicConfig"):
            try:
                main.configure_logging(False)
                self.assertFalse(osc.log.isEnabledFor(logging.DEBUG))
                main.configure_logging(True)
                self.assertTrue(osc.log.isEnabledFor(logging.DEBUG))
                self.assertTrue(logging.getLogger("sps").isEnabledFor(logging.DEBUG))
                self.assertFalse(logging.getLogger("websockets").isEnabledFor(logging.DEBUG))
            finally:
                main.configure_logging(False)

    def test_raw_ogb_and_periodic_status_are_debug_records(self):
        receiver = osc.OSC.__new__(osc.OSC)
        receiver.received = 1
        receiver.ogb_received = 0
        receiver.last_received = None
        receiver._logged_addresses = {}
        with self.assertLogs("osc", "DEBUG") as captured:
            receiver._observe_message("/avatar/parameters/OGB/Pen/Test/PenOthers", .5)
            receiver.report_status()
        self.assertEqual(len(captured.records), 2)
        self.assertTrue(all(record.levelno == logging.DEBUG for record in captured.records))


if __name__ == "__main__":
    unittest.main()
