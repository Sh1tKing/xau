"""Offline log rollover regressions; no MT5 initialization or API calls."""
import concurrent.futures
from datetime import datetime, timedelta
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
logging.disable(logging.CRITICAL)
import xauusd_fib_volume_pa_ai3 as ai

base = ai.base


def record_at(moment, message):
    record = logging.LogRecord("rollover_test", logging.INFO, __file__, 1, message, (), None)
    record.created = moment.timestamp()
    return record


class DailyLoggingTests(unittest.TestCase):
    def test_both_files_follow_midnight_and_late_records(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.Logger("rollover_test")
            with patch.object(base, "LOG_DIR", directory), patch.object(base, "logger", logger):
                local = base.DailyDurableFileHandler(directory, "Fib_PA_")
                logger.addHandler(local)
                ai.attach_ai_logger()
                midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                previous = midnight - timedelta(seconds=1)
                try:
                    logger.handle(record_at(previous, "前一天"))
                    logger.handle(record_at(midnight, "新一天"))
                    logger.handle(record_at(previous, "迟到的旧日志"))
                    logger.handle(record_at(midnight, "新一天继续"))
                    for prefix in ("Fib_PA_", "Fib_PA_AI3_"):
                        old_file = Path(directory) / (prefix + previous.strftime("%Y-%m-%d") + ".log")
                        new_file = Path(directory) / (prefix + midnight.strftime("%Y-%m-%d") + ".log")
                        self.assertEqual([line.rsplit(" - ", 1)[-1] for line in old_file.read_text(encoding="utf-8").splitlines()], ["前一天", "迟到的旧日志"])
                        self.assertEqual([line.rsplit(" - ", 1)[-1] for line in new_file.read_text(encoding="utf-8").splitlines()], ["新一天", "新一天继续"])
                finally:
                    for handler in logger.handlers:
                        handler.close()

    def test_idle_days_and_restart_append_existing_records(self):
        with tempfile.TemporaryDirectory() as directory:
            moment = datetime.now().replace(hour=12, minute=0, second=0, microsecond=0) + timedelta(days=3)
            filename = Path(directory) / ("Fib_PA_" + moment.strftime("%Y-%m-%d") + ".log")
            filename.write_text("existing\n", encoding="utf-8")
            for message in ("first", "after restart"):
                handler = base.DailyDurableFileHandler(directory, "Fib_PA_")
                try:
                    handler.handle(record_at(moment, message))
                finally:
                    handler.close()
            self.assertEqual(filename.read_text(encoding="utf-8").splitlines(), ["existing", "first", "after restart"])

    def test_repeated_ai_attachment_does_not_duplicate_records(self):
        with tempfile.TemporaryDirectory() as directory:
            logger = logging.Logger("attachment_test")
            with patch.object(base, "LOG_DIR", directory), patch.object(base, "logger", logger):
                first = ai.attach_ai_logger()
                second = ai.attach_ai_logger()
                try:
                    self.assertEqual(first, second)
                    self.assertEqual(len(logger.handlers), 1)
                    logger.handle(record_at(datetime.now(), "single record"))
                    self.assertEqual(len(Path(first).read_text(encoding="utf-8").splitlines()), 1)
                finally:
                    for handler in logger.handlers:
                        handler.close()

    def test_concurrent_records_remain_in_their_own_day(self):
        with tempfile.TemporaryDirectory() as directory:
            handler = base.DailyDurableFileHandler(directory, "Fib_PA_")
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            previous = midnight - timedelta(seconds=1)
            records = [record_at(previous if i % 2 else midnight, str(i)) for i in range(40)]
            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(handler.handle, records))
                for moment, parity in ((previous, 1), (midnight, 0)):
                    filename = Path(handler.filename_for_date(moment.strftime("%Y-%m-%d")))
                    actual = filename.read_text(encoding="utf-8").splitlines()
                    self.assertCountEqual(actual, [str(i) for i in range(40) if i % 2 == parity])
            finally:
                handler.close()


if __name__ == "__main__":
    unittest.main()
