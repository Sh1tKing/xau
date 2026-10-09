"""Common event contract boundaries, proven against original source frames."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import timedelta
import json
import unittest

from strategies.events import make_entry_event, validate_entry_event
from test_session_retest_facts import fixture, facts, add_bar


class EntryEventTests(unittest.TestCase):
    def setUp(self):
        self.frame, self.now = fixture()
        self.evidence = facts(self.frame, self.now)

    def check(self, evidence=None, now=None, offset=3, frame=None, legacy=False):
        return validate_entry_event(self.evidence if evidence is None else evidence,
                                    self.now if now is None else now, offset,
                                    self.frame if frame is None else frame,
                                    legacy_session=legacy)

    def test_factory_frozen_json_and_both_actions(self):
        for action in ("buy", "sell"):
            frame, now = fixture(side=action)
            evidence = facts(frame, now, action)
            self.assertEqual(validate_entry_event(evidence, now, 3, frame), "")
            event = make_entry_event(**{k: v for k, v in evidence["entry_event"].items() if k != "version"})
            with self.assertRaises(FrozenInstanceError):
                event.action = "sell"
            self.assertEqual(json.loads(json.dumps(event.to_dict())), event.to_dict())
            self.assertEqual(event.event_id, evidence["event_id"])
            self.assertEqual(len(event.source_refs), 8)

    def test_ttl_lower_inclusive_end_exclusive(self):
        self.assertEqual(self.check(), "")
        self.assertEqual(self.check(now=self.now + timedelta(seconds=299.999)), "")
        self.assertTrue(self.check(now=self.now + timedelta(seconds=300)))
        self.assertTrue(self.check(now=self.now - timedelta(microseconds=1)))

    def test_unknown_or_invalid_offset(self):
        for value in (None, True, "3", float("nan"), float("inf"), 15):
            self.assertTrue(self.check(offset=value), repr(value))
        self.assertTrue(self.check(offset=2))
        self.assertTrue(self.check(now=self.now.replace(tzinfo=None)))

    def test_event_and_closed_trigger_agreement(self):
        for path, value in (("event_id", "other"), ("action", "sell")):
            e = deepcopy(self.evidence)
            e[path] = value
            self.assertTrue(self.check(e))
        for path, value in (("confirmed", 1), ("bar_time", True), ("timeframe", "H1"), ("action", "sell")):
            e = deepcopy(self.evidence)
            e["closed_trigger"][path] = value
            self.assertTrue(self.check(e))

    def test_bad_numeric_types_nonfinite_and_extended_ttl(self):
        for key in ("trigger_bar_time", "episode_start_bar_time", "trigger_close_utc", "valid_until_utc"):
            for value in (True, "42", float("nan"), float("inf"), []):
                e = deepcopy(self.evidence)
                e["entry_event"][key] = value
                self.assertTrue(self.check(e), (key, value))
        e = deepcopy(self.evidence)
        e["entry_event"]["valid_until_utc"] += 1
        self.assertTrue(self.check(e))

    def test_sources_absent_duplicate_gap_and_future_context(self):
        e = deepcopy(self.evidence)
        e["entry_event"]["source_refs"].append(e["entry_event"]["source_refs"][0].copy())
        self.assertTrue(self.check(e))
        e = deepcopy(self.evidence)
        e["entry_event"]["source_refs"][0]["bar_time"] -= 30000
        self.assertTrue(self.check(e))
        for frame in (self.frame.drop(index=4), self.frame.iloc[list(range(len(self.frame))) + [-1]], add_bar(self.frame)):
            self.assertTrue(self.check(frame=frame))
        e = deepcopy(self.evidence)
        e["entry_event"]["source_refs"].pop()
        self.assertTrue(self.check(e))

    def test_latest_closed_m5_and_m15(self):
        self.assertTrue(self.check(now=self.now + timedelta(seconds=300), frame=add_bar(self.frame)))
        raw = int(self.frame.time.iloc[-1]) // 900 * 900 - 900
        close = raw + 900 - 3*3600
        event = make_entry_event(event_id="m15/source", region_id="source", episode_start_bar_time=raw,
                                 action="buy", trigger_timeframe="M15", trigger_bar_time=raw,
                                 trigger_close_utc=close, valid_until_utc=close+300,
                                 source_refs=[{"timeframe":"M15", "bar_time":raw}]).to_dict()
        e = {"event_id":event["event_id"], "action":"buy", "entry_event":event,
             "closed_trigger":{"confirmed":True,"timeframe":"M15","bar_time":raw}}
        self.assertEqual(validate_entry_event(e, close, 3), "")
        self.assertTrue(validate_entry_event(e, close+300, 3))

    def test_explicit_legacy_adapter_preserves_original_window(self):
        e = deepcopy(self.evidence)
        del e["entry_event"]
        self.assertTrue(self.check(e))
        self.assertEqual(self.check(e, legacy=True), "")
        del e["retest_bar"]
        self.assertEqual(self.check(e, legacy=True), "")
        for w in e["windows"]:
            w["active"] = False
        self.assertTrue(self.check(e, legacy=True))

    def test_window_edge_and_changed_window_cannot_extend_ttl(self):
        e = deepcopy(self.evidence)
        active = next(w for w in e["windows"] if w["active"])
        active["entry_end_utc"] = (self.now + timedelta(seconds=150)).isoformat()
        e["entry_event"]["valid_until_utc"] = self.now.timestamp()+150
        self.assertEqual(self.check(e, now=self.now+timedelta(seconds=149), legacy=True), "")
        self.assertTrue(self.check(e, now=self.now+timedelta(seconds=150), legacy=True))
        active["entry_end_utc"] = (self.now + timedelta(hours=2)).isoformat()
        self.assertTrue(self.check(e, now=self.now+timedelta(seconds=150), legacy=True))
        active["entry_start_utc"] = (self.now+timedelta(seconds=1)).isoformat()
        self.assertTrue(self.check(e, legacy=True))

    def test_unconfirmed_session_does_not_create_event(self):
        e = facts(self.frame.iloc[:-1], self.now-timedelta(minutes=5))
        self.assertNotIn("entry_event", e)


if __name__ == "__main__":
    unittest.main()
