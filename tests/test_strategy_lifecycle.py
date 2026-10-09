import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from strategies.lifecycle import EventLedger


class EventLedgerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "ledger.json"
        self.ledger = EventLedger(self.path)

    def position(self, ticket=123, event="london/2026-10-09", opened=1000):
        self.assertTrue(self.ledger.reserve(event, 900))
        self.assertTrue(self.ledger.record_position(ticket, event, opened, 180, 600))

    def test_missing_file_and_readonly_methods_do_not_write(self):
        self.assertEqual(self.ledger.error, "")
        self.assertEqual(self.ledger.entry_error("new", 100), "")
        self.assertEqual(self.ledger.snapshot(), {
            "version": 1, "events": {}, "positions": {}, "cooldown_until": 0.0})
        self.assertEqual(self.ledger.due_tickets(100), ())
        self.assertFalse(self.path.exists())

    def test_restart_preserves_consumption_and_deadline(self):
        self.position()
        restarted = EventLedger(self.path)
        self.assertEqual(restarted.error, "")
        self.assertIn("consumed", restarted.entry_error("london/2026-10-09", 100000))
        self.assertFalse(restarted.reserve("london/2026-10-09", 100000))
        self.assertEqual(restarted.due_tickets(11799), ())
        self.assertEqual(restarted.due_tickets(11800), (123,))

    def test_reserve_persists_before_success_and_never_starts_cooldown(self):
        self.assertTrue(self.ledger.reserve("window", 100))
        state = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(state["events"]["window"]["reserved_at_utc"], 100)
        self.assertEqual(state["cooldown_until"], 0)
        self.assertEqual(state["positions"], {})
        self.assertEqual(self.ledger.entry_error("another", 101), "")

    def test_metadata_and_snapshot_are_detached(self):
        metadata = {"max_hold_minutes": 180, "cooldown_seconds": 600,
                    "contract": {"parameters": [1, "two"]}}
        self.assertTrue(self.ledger.reserve("window", 100, metadata))
        metadata["contract"]["parameters"].append(3)
        snapshot = self.ledger.snapshot()
        snapshot["events"]["window"]["metadata"]["contract"]["parameters"].append(4)
        self.assertEqual(self.ledger.snapshot()["events"]["window"]["metadata"]["contract"],
                         {"parameters": [1, "two"]})

    def test_concurrent_duplicate_reservation_has_exactly_one_success(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            results = list(pool.map(lambda _: self.ledger.reserve("same-window", 100), range(48)))
        self.assertEqual(sum(results), 1)
        self.assertEqual(list(EventLedger(self.path).snapshot()["events"]), ["same-window"])

    def test_repeated_position_cannot_reset_expired_deadline(self):
        self.position()
        original = self.ledger.snapshot()
        self.assertTrue(self.ledger.record_position(123, "london/2026-10-09", 20000, 10080, 1000))
        self.assertEqual(self.ledger.snapshot(), original)
        self.assertEqual(self.ledger.due_tickets(20000), (123,))
        self.assertTrue(EventLedger(self.path).record_position(123, "london/2026-10-09", 30000, 180, 600))
        self.assertEqual(EventLedger(self.path).due_tickets(20000), (123,))

    def test_ticket_cannot_be_reassigned_to_another_event(self):
        self.position()
        self.assertTrue(self.ledger.reserve("new-window", 1000))
        self.assertFalse(self.ledger.record_position(123, "new-window", 2000, 180, 600))
        self.assertEqual(self.ledger.snapshot()["positions"]["123"]["event_id"], "london/2026-10-09")

    def test_first_binding_must_match_frozen_reservation_lifecycle(self):
        self.assertTrue(self.ledger.reserve("window", 900, {"max_hold_minutes": 180, "cooldown_seconds": 600}))
        original = self.ledger.snapshot()
        original_bytes = self.path.read_bytes()
        for minutes, seconds in ((10080, 600), (180, 0), (10080, 0)):
            with self.subTest(minutes=minutes, seconds=seconds):
                self.assertFalse(self.ledger.record_position(123, "window", 1000, minutes, seconds))
                self.assertEqual(self.ledger.snapshot(), original)
                self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertTrue(self.ledger.record_position(123, "window", 1000, 180, 600))
        self.assertEqual(self.ledger.due_tickets(11800), (123,))

    def test_saved_position_mismatch_with_reservation_blocks_restart(self):
        import copy
        self.assertTrue(self.ledger.reserve("window", 900, {"max_hold_minutes": 180, "cooldown_seconds": 600}))
        self.assertTrue(self.ledger.record_position(123, "window", 1000, 180, 600))
        original = self.ledger.snapshot()
        for field, value in (("max_hold_minutes", 10080), ("cooldown_seconds", 0)):
            with self.subTest(field=field):
                state = copy.deepcopy(original)
                state["positions"]["123"][field] = value
                raw = json.dumps(state)
                self.path.write_text(raw, encoding="utf-8")
                restarted = EventLedger(self.path)
                self.assertIn("differs from reserved event", restarted.error)
                self.assertFalse(restarted.reserve("new", 2000))
                self.assertEqual(restarted.due_tickets(1000000), ())
                self.assertEqual(restarted.closed_positions(set(), 1000000), [])
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_repeat_matching_contract_keeps_original_deadline_after_restart(self):
        self.assertTrue(self.ledger.reserve("window", 900, {"max_hold_minutes": 180, "cooldown_seconds": 600}))
        self.assertTrue(self.ledger.record_position(123, "window", 1000, 180, 600))
        restarted = EventLedger(self.path)
        original = restarted.snapshot()
        self.assertFalse(restarted.record_position(123, "window", 20000, 10080, 0))
        self.assertTrue(restarted.record_position(123, "window", 20000, 180, 600))
        self.assertEqual(restarted.snapshot(), original)
        self.assertEqual(restarted.due_tickets(11800), (123,))
        self.assertEqual(EventLedger(self.path).due_tickets(11800), (123,))

    def test_partial_reservation_metadata_only_constrains_declared_fields(self):
        self.assertTrue(self.ledger.reserve("hold-only", 900, {"max_hold_minutes": 180}))
        self.assertFalse(self.ledger.record_position(123, "hold-only", 1000, 181, 0))
        self.assertTrue(self.ledger.record_position(123, "hold-only", 1000, 180, 0))
        self.assertTrue(self.ledger.reserve("cooldown-only", 900, {"cooldown_seconds": 600}))
        self.assertFalse(self.ledger.record_position(456, "cooldown-only", 1000, 60, 0))
        self.assertTrue(self.ledger.record_position(456, "cooldown-only", 1000, 60, 600))
        self.assertEqual(EventLedger(self.path).error, "")

    def test_close_starts_cooldown_once_and_keeps_consumed_event(self):
        self.position()
        self.assertEqual(self.ledger.closed_positions(set(), 2000), [123])
        self.assertEqual(self.ledger.snapshot()["cooldown_until"], 2600)
        self.assertIn("cooldown", self.ledger.entry_error("next", 2599))
        self.assertEqual(self.ledger.entry_error("next", 2600), "")
        self.assertEqual(self.ledger.closed_positions(set(), 4000), [])
        self.assertEqual(self.ledger.snapshot()["cooldown_until"], 2600)
        self.assertIn("consumed", self.ledger.entry_error("london/2026-10-09", 4000))
        self.assertEqual(EventLedger(self.path).closed_positions(set(), 5000), [])

    def test_active_tickets_are_preserved_and_latest_cooldown_wins(self):
        self.position()
        self.assertTrue(self.ledger.reserve("other", 900))
        self.assertTrue(self.ledger.record_position(456, "other", 1000, 180, 1200))
        self.assertEqual(self.ledger.closed_positions({123}, 2000), [456])
        self.assertEqual(self.ledger.snapshot()["cooldown_until"], 3200)
        self.assertEqual(self.ledger.closed_positions(set(), 2100), [123])
        self.assertEqual(self.ledger.snapshot()["cooldown_until"], 3200)

    def test_replace_failure_blocks_without_publishing_consumed_event(self):
        original = self.ledger.snapshot()
        with patch("strategies.lifecycle.os.replace", side_effect=OSError("replace denied")):
            self.assertFalse(self.ledger.reserve("window", 100))
        self.assertIn("replace denied", self.ledger.error)
        self.assertEqual(self.ledger.snapshot(), original)
        self.assertFalse(self.path.exists())
        self.assertFalse(self.ledger.reserve("other", 101))
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])

    def test_fsync_failure_preserves_existing_disk_and_memory(self):
        self.position()
        original_bytes = self.path.read_bytes()
        original_state = self.ledger.snapshot()
        with patch("strategies.lifecycle.os.fsync", side_effect=OSError("disk error")):
            self.assertFalse(self.ledger.reserve("new", 2000))
        self.assertEqual(self.path.read_bytes(), original_bytes)
        self.assertEqual(self.ledger.snapshot(), original_state)
        self.assertEqual(self.ledger.due_tickets(11800), (123,))
        self.assertFalse(self.ledger.reserve("new", 2000))

    def test_atomic_write_is_same_directory_and_fsynced_before_replace(self):
        operations = []
        from strategies import lifecycle
        real_fsync, real_replace = lifecycle.os.fsync, lifecycle.os.replace

        def fsync(fd):
            operations.append("fsync")
            return real_fsync(fd)

        def replace(source, target):
            operations.append("replace")
            self.assertEqual(Path(source).parent, self.path.parent)
            self.assertEqual(json.loads(Path(source).read_text(encoding="utf-8"))["events"].keys(), {"window"})
            return real_replace(source, target)

        with patch("strategies.lifecycle.os.fsync", side_effect=fsync), patch("strategies.lifecycle.os.replace", side_effect=replace):
            self.assertTrue(self.ledger.reserve("window", 100))
        self.assertEqual(operations, ["fsync", "replace"])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_position_write_failure_sets_error_and_preserves_state(self):
        self.assertTrue(self.ledger.reserve("window", 100))
        original = self.ledger.snapshot()
        with patch("strategies.lifecycle.os.replace", side_effect=OSError("position failed")):
            self.assertFalse(self.ledger.record_position(123, "window", 1000, 180, 600))
        self.assertEqual(self.ledger.snapshot(), original)
        self.assertIn("position failed", self.ledger.error)

    def test_close_write_failure_keeps_deadline_available_for_guardian(self):
        self.position()
        with patch("strategies.lifecycle.os.replace", side_effect=OSError("close failed")):
            self.assertEqual(self.ledger.closed_positions(set(), 12000), [])
        self.assertEqual(self.ledger.due_tickets(12000), (123,))
        self.assertEqual(self.ledger.snapshot()["cooldown_until"], 0)
        self.assertIn("close failed", self.ledger.error)
        self.assertFalse(self.ledger.reserve("new", 13000))

    def test_malformed_and_nonfinite_state_block_entries_without_rewriting(self):
        bad_states = ["{", "[]", "null", '{"version":1,"events":{},"positions":{},"cooldown_until":NaN}',
                      '{"version":1,"events":{},"positions":{},"cooldown_until":-1}',
                      '{"version":1,"version":1,"events":{},"positions":{},"cooldown_until":0}']
        for raw in bad_states:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding="utf-8")
                ledger = EventLedger(self.path)
                self.assertIn("load failed", ledger.error)
                self.assertFalse(ledger.reserve("window", 100))
                self.assertEqual(ledger.due_tickets(100000), ())
                self.assertEqual(ledger.closed_positions(set(), 100000), [])
                self.assertEqual(self.path.read_text(encoding="utf-8"), raw)

    def test_invalid_saved_events_and_positions_block_load(self):
        self.position()
        original = self.ledger.snapshot()
        mutations = [lambda s: s["events"]["london/2026-10-09"].update(reserved_at_utc=-1),
                     lambda s: s["events"]["london/2026-10-09"].update(metadata={"contract": float("inf")}),
                     lambda s: s["positions"]["123"].update(opened_at_utc=0),
                     lambda s: s["positions"]["123"].update(max_hold_minutes=True),
                     lambda s: s["positions"]["123"].update(cooldown_seconds=-1),
                     lambda s: s["positions"]["123"].update(event_id="unknown"),
                     lambda s: s["positions"].update({"0123": s["positions"].pop("123")})]
        import copy
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                state = copy.deepcopy(original)
                mutate(state)
                self.path.write_text(json.dumps(state), encoding="utf-8")
                self.assertIn("load failed", EventLedger(self.path).error)

    def test_invalid_ids_and_times_do_not_consume_or_poison_valid_entry(self):
        for event in (None, "", " ", " padded", "control\n", True, 123):
            self.assertFalse(self.ledger.reserve(event, 100))
            self.assertIn("invalid", self.ledger.entry_error(event, 100))
        for now in (0, -1, True, None, "100", float("nan"), float("inf"), 10**1000):
            self.assertFalse(self.ledger.reserve("window", now))
            self.assertIn("invalid", self.ledger.entry_error("window", now))
            self.assertEqual(self.ledger.due_tickets(now), ())
        self.assertEqual(self.ledger.error, "")
        self.assertEqual(self.ledger.snapshot()["events"], {})
        self.assertTrue(self.ledger.reserve("window", 100))

    def test_invalid_metadata_is_not_consumed(self):
        recursive = {}
        recursive["loop"] = recursive
        for metadata in ({"v": float("nan")}, {1: "integer key"}, {"v": (1, 2)},
                         {"max_hold_minutes": True}, {"cooldown_seconds": -1}, recursive, []):
            self.assertFalse(self.ledger.reserve("window", 100, metadata))
        self.assertEqual(self.ledger.snapshot()["events"], {})
        self.assertTrue(self.ledger.reserve("window", 100))

    def test_invalid_position_parameters_and_unreserved_events_refused(self):
        self.assertFalse(self.ledger.record_position(123, "unknown", 1000, 180, 600))
        self.assertTrue(self.ledger.reserve("window", 100))
        for args in ((True, "window", 1000, 180, 600), (0, "window", 1000, 180, 600),
                     (123, "window", 0, 180, 600), (123, "window", 1000, 0, 600),
                     (123, "window", 1000, 180, True), (123, "window", 1000, 180, -1),
                     (123, "window", 1000, 10081, 600), (123, "window", 1000, 180, 604801)):
            self.assertFalse(self.ledger.record_position(*args))
        self.assertEqual(self.ledger.snapshot()["positions"], {})

    def test_invalid_active_snapshot_does_not_mark_everything_closed(self):
        self.position()
        original = self.ledger.snapshot()
        for active in (None, [], {True}, {0}, {-1}):
            self.assertEqual(self.ledger.closed_positions(active, 1000), [])
        self.assertEqual(self.ledger.closed_positions(set(), float("nan")), [])
        self.assertEqual(self.ledger.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
