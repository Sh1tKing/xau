"""Dated official fixtures and synthetic clock/cache failures; no network."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from strategies import news_calendar as n

FIXTURES = Path(__file__).parent/"fixtures/news_calendar"


def schedules(now, events=()):
    return [n.frame(provider, [e for e in events if e["family"] in families],
                    now-86400*10, now+86400*10, now, )
            for provider, families in n.FAMILIES.items()]


class CalendarTests(unittest.TestCase):
    def setUp(self):
        self.now = n.instant("2026-10-14T12:30:00Z")
        self.cpi = n.event("cpi", self.now, n.BLS_URL)

    def test_exact_15_minute_boundaries_and_30_minute_window(self):
        frames = schedules(self.now, [self.cpi])
        for delta, allowed in [(-900.01, True), (-900, False), (-1, False),
                               (0, False), (899.99, False), (900, True)]:
            with self.subTest(delta=delta):
                # A genuine schedule is verified before the first boundary.
                for f in frames:
                    f["verified_at"] = self.now-3600
                result = n.evaluate(frames, self.now+delta)
                self.assertEqual(result.allowed, allowed)
                if not allowed:
                    self.assertEqual(result.resume_at, self.now+900)
                    self.assertIn("2026-10-14 20:15:00", result.reason)
                    self.assertIn("2026-10-14 20:45:00", result.reason)

    def test_overlapping_and_touching_windows_merge_decision_and_press_conference(self):
        events = [n.event("fomc_decision", self.now, n.FED_BASE),
                  n.event("fomc_press_conference", self.now+1800, n.FED_BASE)]
        frames = schedules(self.now-3600, events)
        result = n.evaluate(frames, self.now+900)
        self.assertFalse(result.allowed)
        self.assertEqual(result.resume_at, self.now+2700)
        self.assertEqual(len(result.events), 2)
        self.assertTrue(n.evaluate(frames, self.now+2700).allowed)

    def test_old_proposal_crossing_whole_pause_must_not_resume(self):
        frames = schedules(self.now-3600, [self.cpi])
        self.assertFalse(n.evaluate(frames, self.now+900, self.now-901).allowed)
        self.assertTrue(n.evaluate(frames, self.now+901, self.now+900).allowed)

    def test_utc_new_york_dst_and_beijing_day_rollover(self):
        summer = n.instant("2026-10-28T14:00:00-04:00")
        winter = n.instant("2026-12-09T14:00:00-05:00")
        self.assertEqual(n.local_time(summer), "2026-10-29 02:00:00")
        self.assertEqual(n.local_time(winter), "2026-12-10 03:00:00")
        self.assertEqual(n.local_time(n.instant("2026-11-06T08:30:00-05:00")), "2026-11-06 21:30:00")

    def test_missing_stale_future_and_wrong_coverage_fail_closed(self):
        for mutate in [lambda f: f.pop(), lambda f: f[0].update(verified_at=self.now-n.MAX_AGE_SECONDS-1),
                       lambda f: f[0].update(verified_at=self.now+6),
                       lambda f: f[0].update(coverage_until=self.now)]:
            frames = schedules(self.now)
            mutate(frames)
            self.assertFalse(n.evaluate(frames, self.now).allowed)

    def test_nonfinite_bad_family_bad_sources_and_naive_dates_rejected(self):
        for stamp in ["2026-10-14T08:30:00", "Tentative", "", None]:
            with self.assertRaises(n.CalendarError):
                n.instant(stamp)
        for value in [float("nan"), float("inf"), True, -2, 0]:
            self.assertFalse(n.evaluate(schedules(self.now), value).allowed)
        for mutate in [lambda f: f[0]["events"][0].update(family="pce"),
                       lambda f: f[0]["events"][0].update(at=float("nan")),
                       lambda f: f[0]["events"][0].update(source="http://not-secure.test")]:
            frames = schedules(self.now, [self.cpi])
            mutate(frames)
            with self.assertRaises(n.CalendarError):
                n.evaluate(frames, self.now)

    def test_no_events_is_allowed_only_with_all_three_verified_coverages(self):
        self.assertTrue(n.evaluate(schedules(self.now), self.now).allowed)
        self.assertFalse(n.evaluate([], self.now).allowed)

    def test_bls_ics_tzid_utc_folding_cancellations(self):
        content = """BEGIN:VCALENDAR
BEGIN:VEVENT
SUMMARY:Consumer Price Index
 for September 2026
DTSTART;TZID=America/New_York:20261014T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Employment Situation
DTSTART:20261106T133000Z
END:VEVENT
BEGIN:VEVENT
SUMMARY:Producer Price Index
DTSTART;TZID=US/Eastern:20261015T083000
END:VEVENT
BEGIN:VEVENT
SUMMARY:Consumer Price Index cancelled
DTSTART:20261016T123000Z
STATUS:CANCELLED
END:VEVENT
END:VCALENDAR"""
        result = n.parse_bls(content, self.now)
        self.assertEqual(len(result["events"]), 3)
        times = {e["family"]: e["at"] for e in result["events"]}
        self.assertEqual(times["cpi"], self.now)
        self.assertEqual(n.local_time(times["nfp"]), "2026-11-06 21:30:00")
        for bad in [content.replace("END:VCALENDAR", ""),
                    content.replace("TZID=America/New_York", "TZID=Missing/Timezone"),
                    content.replace("DTSTART;TZID=America/New_York:20261014T083000", "DTSTART;VALUE=DATE:20261014")]:
            with self.assertRaises(n.CalendarError):
                n.parse_bls(bad, self.now)

    def test_weekly_fallback_explicit_iso_date_currency_and_week_coverage(self):
        payload = [{"title": "CPI m/m", "country": "USD", "date": "2026-10-14T08:30:00-04:00"},
                   {"title": "CPI m/m", "country": "GBP", "date": "2026-10-12T08:30:00-04:00"},
                   {"title": "Core PPI m/m", "country": "USD", "date": "2026-10-15T08:30:00-04:00"}]
        result = n.parse_ff_bls(payload, self.now)
        self.assertEqual([e["family"] for e in result["events"]], ["cpi", "ppi"])
        self.assertEqual(n.local_time(result["coverage_until"]), "2026-10-18 12:00:00")
        for bad in [[], payload+[dict(payload[0], date="2026-10-21T08:30:00-04:00")],
                    [dict(payload[0], date="2026-10-14T08:30:00")]]:
            with self.assertRaises(n.CalendarError):
                n.parse_ff_bls(bad, self.now)

    def test_bea_official_national_keys_ignore_state_gdp_and_duplicate_dates(self):
        payload = json.loads((FIXTURES/"bea_national.json").read_text(encoding="utf-8"))
        payload["Gross Domestic Product by State"] = {"release_dates": ["2026-10-14T12:30:00Z"]}
        payload["Gross Domestic Product"]["release_dates"].append(payload["Gross Domestic Product"]["release_dates"][0])
        result = n.parse_bea(payload, self.now)
        events = [e for e in result["events"] if n.local_time(e["at"]).startswith("2026-10")]
        self.assertEqual(len(events), 2)
        self.assertEqual({n.local_time(e["at"]) for e in events}, {"2026-10-29 20:30:00"})
        for bad in [{}, {"Gross Domestic Product": {"release_dates": []}}]:
            with self.assertRaises(n.CalendarError):
                n.parse_bea(bad, self.now)

    def test_fed_official_rows_release_day_dst_dedup_and_minutes(self):
        for month, expected in [(10, "2026-10-29 02:00:00"), (12, "2026-12-10 03:00:00")]:
            text = (FIXTURES/f"fed_2026_{month:02}.html").read_text(encoding="utf-8")
            value = n.parse_fed(text, 2026, month, self.now)
            decision = next(e for e in value["events"] if e["family"] == "fomc_decision")
            self.assertEqual(n.local_time(decision["at"]), expected)
            self.assertEqual(len(value["events"]), 3)
            with self.assertRaises(n.CalendarError):
                n.parse_fed(text.replace("2:00 p.m.", "Tentative"), 2026, month, self.now)
        november = n.parse_fed((FIXTURES/"fed_2026_11.html").read_text(), 2026, 11, self.now)
        self.assertEqual([e["family"] for e in november["events"]], ["fomc_minutes"])
        with self.assertRaises(n.CalendarError):
            n.parse_fed("<html>Access denied</html>", 2026, 10, self.now)

    def test_seed_contains_every_promised_event_family_with_finite_expiry(self):
        seed = Path(__file__).resolve().parents[1]/"strategies/calendars/us_major_events.json"
        value = json.loads(seed.read_text(encoding="utf-8"))
        for frame in value["frames"]:
            n.validate_frame(frame)
        self.assertEqual({e["family"] for f in value["frames"] for e in f["events"]},
                         set.union(*n.FAMILIES.values()))
        clock = max(f["verified_at"] for f in value["frames"])
        self.assertTrue(n.evaluate(value["frames"], clock).allowed)
        self.assertFalse(n.evaluate(value["frames"], clock+n.MAX_AGE_SECONDS+1).allowed)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/"news.json"
        self.now = n.instant("2026-10-14T11:00:00Z")
        self.frames = schedules(self.now)

    def manager(self, fetcher=None):
        obj = n.NewsCalendar(self.path, fetcher=fetcher or (lambda p,t: next(f for f in self.frames if f["provider"] == p)))
        return obj

    def test_refresh_atomic_cache_restart_and_unavailable_source_keep_valid_data(self):
        obj = self.manager()
        self.assertTrue(obj.refresh(self.now))
        restarted = self.manager(Mock(side_effect=ConnectionError("mock")))
        restarted.request_refresh = lambda: None
        self.assertTrue(restarted.status(self.now).allowed)
        self.assertFalse(restarted.refresh(self.now+10))
        self.assertTrue(restarted.status(self.now+10).allowed)
        self.assertFalse(restarted.status(self.now+n.MAX_AGE_SECONDS+1).allowed)
        self.assertEqual(list(self.path.parent.glob(".news-calendar-*")), [])

    def test_partial_refresh_never_erases_other_providers_and_recovers(self):
        obj = self.manager()
        obj.refresh(self.now)
        def partial(provider, observed):
            if provider == "bls":
                raise ConnectionError("mock")
            return next(f for f in schedules(observed) if f["provider"] == provider)
        obj.fetcher = partial
        self.assertFalse(obj.refresh(self.now+3600))
        obj.request_refresh = lambda: None
        self.assertTrue(obj.status(self.now+3600).allowed)
        self.assertFalse(obj.status(self.now+n.MAX_AGE_SECONDS+1).allowed)
        obj.fetcher = lambda p,t: next(f for f in schedules(t) if f["provider"] == p)
        obj.refresh(self.now+n.MAX_AGE_SECONDS+2)
        self.assertTrue(obj.status(self.now+n.MAX_AGE_SECONDS+2).allowed)

    def test_background_fetch_never_blocks_guard_thread_and_deduplicates_workers(self):
        entered, release = threading.Event(), threading.Event()
        def slow(provider, observed):
            entered.set()
            release.wait(1)
            return next(f for f in schedules(observed) if f["provider"] == provider)
        obj = self.manager(slow)
        obj._frames = self.frames
        started = time.monotonic()
        result = obj.status(self.now)
        self.assertLess(time.monotonic()-started, .2)
        self.assertTrue(result.allowed)
        self.assertTrue(entered.wait(.5))
        obj.request_refresh()
        self.assertTrue(obj._busy)
        release.set()
        deadline = time.monotonic()+2
        while obj._busy and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(obj._busy)

    def test_bad_cache_falls_back_to_valid_seed_without_refreshing_its_age(self):
        seed = self.path.parent/"seed.json"
        seed.write_text(json.dumps({"format": 1, "frames": self.frames}))
        self.path.write_text("{invalid")
        obj = n.NewsCalendar(self.path, seed, fetcher=Mock(side_effect=ConnectionError("mock")))
        obj.request_refresh = lambda: None
        self.assertTrue(obj.status(self.now).allowed)
        obj.refresh(self.now+1)
        self.assertFalse(obj.status(self.now+n.MAX_AGE_SECONDS+1).allowed)

    def test_slow_cache_fsync_never_holds_lock_used_inside_broker_entry_checks(self):
        entered, release = threading.Event(), threading.Event()
        obj = self.manager()
        obj._frames = self.frames
        # This lock test uses dated synthetic frames, including in its worker.
        # A real wall clock would stop reaching fsync after fixture expiry.
        refresh = obj.refresh
        obj.refresh = lambda: refresh(self.now)
        original = obj._persist
        def slow(snapshots):
            entered.set()
            release.wait(1)
            original(snapshots)
        obj._persist = slow
        obj.request_refresh()
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        self.assertTrue(obj.status(self.now).allowed)
        self.assertLess(time.monotonic()-started, .2)
        release.set()
        deadline = time.monotonic()+2
        while obj._busy and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertFalse(obj._busy)

    def test_write_failure_keeps_valid_in_memory_snapshot(self):
        obj = self.manager()
        with patch.object(obj, "_persist", side_effect=PermissionError("mock")):
            self.assertTrue(obj.refresh(self.now))
        obj.request_refresh = lambda: None
        self.assertTrue(obj.status(self.now).allowed)

    def test_disabled_never_starts_network_and_bad_env_is_explicit(self):
        with patch.dict(n.os.environ, {"FIB_PA_NEWS_PAUSE_ENABLED": "false"}):
            obj = n.NewsCalendar.from_environment(self.tmp.name, self.tmp.name)
        with patch.object(obj, "fetcher") as fetch, patch.object(n.threading.Thread, "start") as start:
            self.assertTrue(obj.status(self.now).allowed)
            start.assert_not_called()
            fetch.assert_not_called()
        with patch.dict(n.os.environ, {"FIB_PA_NEWS_PAUSE_ENABLED": "yes"}):
            bad = n.NewsCalendar.from_environment(self.tmp.name, self.tmp.name)
        self.assertFalse(bad.status(self.now).allowed)
        self.assertIn("配置错误", bad.status(self.now).reason)


if __name__ == "__main__":
    unittest.main()
