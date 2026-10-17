"""Scheduled US releases, UTC entry pauses and asynchronous public-calendar cache.

No MT5, AI, account or credential access. HTTP runs only in the refresh worker
or an explicitly requested CLI refresh. Missing calendars stop entries only.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import calendar
import copy
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

VERSION = "news-pause-v1"
FORMAT = 1
WINDOW_SECONDS = 15 * 60
MAX_AGE_SECONDS = 7 * 86400
REFRESH_SECONDS = 6 * 3600
RETRY_SECONDS = 15 * 60
READINESS_VERSION = "news-coverage-v1"
NY = ZoneInfo("America/New_York")
BJ = ZoneInfo("Asia/Shanghai")
BLS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
BEA_URL = "https://apps.bea.gov/API/signup/release_dates.json"
FED_BASE = "https://www.federalreserve.gov/newsevents/"
FAMILIES = {
    "bls": {"nfp", "cpi", "ppi"},
    "bea": {"gdp", "pce"},
    "fed": {"fomc_decision", "fomc_press_conference", "fomc_minutes"},
}
LABELS = {"nfp": "美国非农", "cpi": "美国CPI", "ppi": "美国PPI",
          "gdp": "美国GDP", "pce": "美国PCE", "fomc_decision": "美联储利率决议",
          "fomc_press_conference": "美联储发布会", "fomc_minutes": "FOMC会议纪要"}


class CalendarError(ValueError):
    pass


def instant(value):
    if not isinstance(value, str) or not value.strip():
        raise CalendarError("发布时间必须带时区")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CalendarError("发布时间格式错误") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise CalendarError("发布时间缺少时区，禁止猜北京时间或服务器时间")
    return dt.timestamp()


def utc(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def local_time(value):
    return datetime.fromtimestamp(value, BJ).strftime("%Y-%m-%d %H:%M:%S")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def _source(value):
    parsed = urlsplit(value) if isinstance(value, str) else None
    return bool(parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password)


def frame(provider, events, start, end, verified_at):
    unique = {(e["family"], e["at"]): copy.deepcopy(e) for e in events}
    value = {"provider": provider, "verified_at": float(verified_at),
             "coverage_from": float(start), "coverage_until": float(end),
             "events": sorted(unique.values(), key=lambda e: (e["at"], e["family"]))}
    validate_frame(value)
    return value


def validate_frame(value):
    if not isinstance(value, dict) or value.get("provider") not in FAMILIES:
        raise CalendarError("日历来源类别错误")
    for key in ("verified_at", "coverage_from", "coverage_until"):
        if not _number(value.get(key)):
            raise CalendarError("日历时间无效: " + key)
    if value["coverage_from"] >= value["coverage_until"] or not isinstance(value.get("events"), list):
        raise CalendarError("日历覆盖期限或事件列表错误")
    for e in value["events"]:
        if not isinstance(e, dict) or e.get("family") not in FAMILIES[value["provider"]]:
            raise CalendarError("日历事件类别错误")
        if not _number(e.get("at")) or not value["coverage_from"] <= e["at"] < value["coverage_until"]:
            raise CalendarError("事件超出声明日历范围")
        if not isinstance(e.get("title"), str) or not e["title"].strip() or not _source(e.get("source")):
            raise CalendarError("事件名称或来源错误")
    return value


def event(family, at, source, title=None):
    return {"family": family, "at": float(at), "title": title or LABELS[family], "source": source}


def _bls_family(title):
    title = title.lower().strip()
    if title.startswith(("employment situation", "non-farm employment change", "nonfarm payroll")):
        return "nfp"
    if title.startswith(("consumer price index", "core cpi", "cpi ")):
        return "cpi"
    if title.startswith(("producer price index", "core ppi", "ppi ")):
        return "ppi"
    return None


def parse_bls(text, observed):
    if "BEGIN:VCALENDAR" not in text or "END:VCALENDAR" not in text:
        raise CalendarError("BLS未返回完整订阅日历")
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    rows, fields = [], None
    for line in lines:
        if line == "BEGIN:VEVENT":
            fields = {}
        elif line == "END:VEVENT" and fields is not None:
            title = fields.get("SUMMARY", ("", ""))[1]
            family = _bls_family(title)
            if family and fields.get("STATUS", ("", ""))[1] != "CANCELLED":
                key, raw = fields.get("DTSTART", ("", ""))
                if "VALUE=DATE" in key or not re.fullmatch(r"\d{8}T\d{6}Z?", raw):
                    raise CalendarError("BLS重要事件发布时间缺失或非精确时间")
                dt = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
                if raw.endswith("Z"):
                    dt = dt.replace(tzinfo=timezone.utc)
                else:
                    match = re.search(r'(?:^|;)TZID="?([^";]+)"?', key)
                    if not match:
                        raise CalendarError("BLS事件缺少TZID")
                    try:
                        dt = dt.replace(tzinfo=ZoneInfo(match[1]))
                    except Exception as exc:
                        raise CalendarError("BLS事件时区无效") from exc
                rows.append(event(family, dt.timestamp(), BLS_URL, title.replace(r"\,", ",")))
            fields = None
        elif fields is not None and ":" in line:
            key, value = line.split(":", 1)
            fields[key.split(";", 1)[0]] = (key, value)
    if fields is not None or {e["family"] for e in rows} != FAMILIES["bls"]:
        raise CalendarError("BLS日历不完整，缺少非农/CPI/PPI")
    first, last = min(e["at"] for e in rows), max(e["at"] for e in rows)
    return frame("bls", rows, first-WINDOW_SECONDS, last+86400, observed)


def parse_ff_bls(payload, observed):
    """The site's complete weekly export is fallback coverage for BLS only."""
    if not isinstance(payload, list) or not payload:
        raise CalendarError("备用周历为空或格式错误")
    dates, rows = [], []
    for row in payload:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str):
            raise CalendarError("备用周历事件格式错误")
        at = instant(row.get("date"))
        dates.append(datetime.fromtimestamp(at, NY))
        family = _bls_family(row["title"]) if row.get("country") == "USD" else None
        if family:
            rows.append(event(family, at, FF_URL, row["title"]))
    first = min(dates).date()
    sunday = first - timedelta(days=(first.weekday()+1) % 7)
    start = datetime.combine(sunday, datetime.min.time(), NY)
    end = start + timedelta(days=7)
    if any(not start <= d < end for d in dates):
        raise CalendarError("备用周历跨多个周或混入旧数据")
    return frame("bls", rows, start.timestamp(), end.timestamp(), observed)


def parse_bea(payload, observed):
    if not isinstance(payload, dict):
        raise CalendarError("BEA日历格式错误")
    rows = []
    last_dates = []
    for title, family in (("Gross Domestic Product", "gdp"), ("Personal Income and Outlays", "pce")):
        info = payload.get(title)
        dates = info.get("release_dates") if isinstance(info, dict) else None
        if not isinstance(dates, list) or not dates:
            raise CalendarError("BEA缺少全国GDP/PCE")
        times = [instant(d) for d in dates]
        last_dates.append(max(times))
        rows.extend(event(family, at, BEA_URL, title) for at in times)
    # Each required series must cover the entire accepted interval.
    end = min(last_dates)+86400
    rows = [e for e in rows if e["at"] < end]
    return frame("bea", rows, min(e["at"] for e in rows)-WINDOW_SECONDS, end, observed)


class _FedRows(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.rows, self.text = [], [], []
        self.row, self.column = None, None

    def handle_starttag(self, tag, attrs):
        if tag != "div":
            return
        classes = dict(attrs).get("class", "").split()
        self.stack.append((self.row, self.column))
        if "row" in classes:
            self.row, self.column = [[], [], []], None
        if self.row is not None:
            for i, name in enumerate(("col-xs-2", "col-xs-7", "col-xs-3")):
                if name in classes:
                    self.column = i

    def handle_endtag(self, tag):
        if tag == "div" and self.stack:
            previous_row, previous_column = self.stack.pop()
            if self.row is not None and previous_row is not self.row:
                self.rows.append([" ".join(c).strip() for c in self.row])
            self.row, self.column = previous_row, previous_column

    def handle_data(self, data):
        self.text.append(data)
        if self.row is not None and self.column is not None:
            self.row[self.column].append(data.strip())


def parse_fed(text, year, month, observed):
    parsed = _FedRows()
    parsed.feed(text)
    joined = " ".join(parsed.text)
    if f"Calendar: {calendar.month_name[month]} {year}" not in joined or "FOMC Meetings" not in joined:
        raise CalendarError("美联储月历未发布完整会议部分或月份不符")
    rows = []
    source = FED_BASE + f"{year}-{calendar.month_name[month].lower()}.htm"
    for clock, title, days in parsed.rows:
        family = ("fomc_minutes" if title.startswith("FOMC Minutes") else
                  "fomc_press_conference" if title.startswith("FOMC Press Conference") else
                  "fomc_decision" if title.startswith("FOMC Meeting") else None)
        if family is None:
            continue
        match = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([ap])\.m\.", clock)
        if not match or not re.fullmatch(r"\d{1,2}", days):
            raise CalendarError("美联储重要事件时间不明确，不按两日会议首日猜测")
        hour, minute = int(match[1]), int(match[2])
        if not 1 <= hour <= 12 or not 0 <= minute < 60:
            raise CalendarError("美联储发布时间非法")
        at = datetime(year, month, int(days), hour % 12 + (12 if match[3] == "p" else 0), minute, tzinfo=NY)
        rows.append(event(family, at.timestamp(), source))
    start = datetime(year, month, 1, tzinfo=NY)
    end = datetime(year+month//12, month % 12+1, 1, tzinfo=NY)
    return frame("fed", rows, start.timestamp(), end.timestamp(), observed)


@dataclass(frozen=True)
class PauseStatus:
    allowed: bool
    reason: str = ""
    resume_at: float | None = None
    events: tuple = ()
    degraded: bool = False


def evaluate(frames, now, since=None):
    if not _number(now) or (since is not None and (not _number(since) or since > now+1)):
        return PauseStatus(False, "新闻日历时钟/入场任务时间无效")
    chosen, degraded = {}, False
    for provider in FAMILIES:
        candidates = []
        for value in frames:
            validate_frame(value)
            if value["provider"] == provider and value["coverage_from"] <= now < value["coverage_until"]:
                age = now-value["verified_at"]
                if -5 <= age <= MAX_AGE_SECONDS:
                    candidates.append(value)
        if not candidates:
            return PauseStatus(False, f"新闻日历不可用/过期/不覆盖当前时间: {provider}；暂停新入场，护盘继续")
        chosen[provider] = max(candidates, key=lambda v: v["verified_at"])
        if since is not None and since < chosen[provider]["coverage_from"]:
            return PauseStatus(False, "入场任务早于已核对日历覆盖范围，等待新行情分析")
        degraded |= now-chosen[provider]["verified_at"] > REFRESH_SECONDS
    intervals = []
    for value in chosen.values():
        for e in value["events"]:
            start, end = e["at"]-WINDOW_SECONDS, e["at"]+WINDOW_SECONDS
            intervals.append((start, end, e["title"]))
    merged = []
    for start, end, title in sorted(intervals):
        if merged and start <= merged[-1][1]:
            previous = merged[-1]
            merged[-1] = (previous[0], max(previous[1], end), previous[2] | {title})
        else:
            merged.append((start, end, {title}))
    for start, end, titles in merged:
        if start <= now < end:
            names = tuple(sorted(titles))
            return PauseStatus(False, "新闻休息: " + "、".join(names) + "；北京时间 " +
                               local_time(start) + " 至 " + local_time(end), end, names, degraded)
        if since is not None and since < end and now >= end and start <= now and since < now:
            names = tuple(sorted(titles))
            return PauseStatus(False, "入场任务跨过新闻休息窗口，丢弃旧提案并等待新行情分析: " +
                               "、".join(names), None, names, degraded)
    return PauseStatus(True, degraded=degraded)


def coverage_report(frames, now, through=None, seed=()):
    """Describe continuous verified coverage, not permission to trade through news.

    A long coverage_until cannot extend the seven-day verification lifetime.
    Cache and bootstrap are both included; future unobserved verifications are not.
    """
    through = now if through is None else through
    if not _number(now) or not _number(through) or through < now:
        raise CalendarError("检查终点必须是当前或未来的带时区时刻")
    providers = {}
    candidates = [(v,"cache") for v in frames]+[(v,"bootstrap") for v in seed]
    for provider in FAMILIES:
        rows=[]
        for value,kind in candidates:
            validate_frame(value)
            if value["provider"] != provider or value["verified_at"] > now+5:
                continue
            end=min(value["coverage_until"],value["verified_at"]+MAX_AGE_SECONDS)
            if end > now:
                rows.append((value["coverage_from"],end,value,kind))
        cursor=now
        used=[]
        while True:
            available=[r for r in rows if r[0] <= cursor < r[1]]
            if not available:
                break
            selected=max(available,key=lambda r:(r[1],r[2]["verified_at"]))
            cursor=selected[1]
            used.append({"kind":selected[3],"verified_at_utc":utc(selected[2]["verified_at"]),
                         "usable_until_beijing":local_time(selected[1]),
                         "sources":sorted({e["source"] for e in selected[2]["events"]})})
        providers[provider]={"available_now":cursor > now,"covers_through":cursor > through,
                             "usable_until_utc":utc(cursor) if cursor > now else None,
                             "usable_until_beijing":local_time(cursor) if cursor > now else None,
                             "segments":used}
    return {"version":READINESS_VERSION,"checked_at_utc":utc(now),"through_utc":utc(through),
            "available_now":all(p["available_now"] for p in providers.values()),
            "covers_through":all(p["covers_through"] for p in providers.values()),"providers":providers}


def _get(url):
    response = requests.get(url, timeout=(5, 12), headers={"User-Agent": "MYQ-Gold-News-Calendar/1"})
    response.raise_for_status()
    if len(response.content) > 2_000_000:
        raise CalendarError("日历响应过大")
    return response


def fetch_provider(provider, now):
    if provider == "bls":
        try:
            return parse_bls(_get(BLS_URL).content.decode("utf-8-sig"), now)
        except (requests.RequestException, CalendarError, UnicodeError):
            return parse_ff_bls(_get(FF_URL).json(), now)
    if provider == "bea":
        return parse_bea(_get(BEA_URL).json(), now)
    local = datetime.fromtimestamp(now, NY)
    url = FED_BASE + f"{local.year}-{calendar.month_name[local.month].lower()}.htm"
    return parse_fed(_get(url).content.decode("utf-8-sig"), local.year, local.month, now)


class NewsCalendar:
    def __init__(self, cache_path, seed_path=None, enabled=True, logger=None, fetcher=fetch_provider):
        self.cache_path, self.logger, self.fetcher = Path(cache_path), logger, fetcher
        self.enabled, self.error = enabled, ""
        self._lock = threading.RLock()
        self._frames, self._seed, self._busy, self._next_refresh = [], [], False, 0.
        self._last_log = None
        for path, seed in ((self.cache_path, False), (Path(seed_path) if seed_path else None, True)):
            if path is None or not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8-sig"))
                if value.get("format") != FORMAT or not isinstance(value.get("frames"), list):
                    raise CalendarError("日历缓存版本/结构错误")
                snapshots = [copy.deepcopy(validate_frame(v)) for v in value["frames"]]
                if seed:
                    self._seed = snapshots
                else:
                    self._frames = snapshots
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self._log("warning", "新闻日历缓存读取失败: %s", type(exc).__name__)

    @classmethod
    def from_environment(cls, base_dir, log_dir, logger=None):
        setting = os.getenv("FIB_PA_NEWS_PAUSE_ENABLED", "true").strip().lower()
        instance = cls(os.getenv("FIB_PA_NEWS_CALENDAR_CACHE", "").strip() or Path(log_dir)/"news_calendar.json",
                       Path(base_dir)/"strategies/calendars/us_major_events.json",
                       enabled=setting != "false", logger=logger)
        if setting not in ("true", "false"):
            instance.error = "FIB_PA_NEWS_PAUSE_ENABLED只接受true/false"
        return instance

    def _log(self, level, text, *args):
        if self.logger:
            getattr(self.logger, level)(text, *args)

    def refresh(self, now=None):
        now = time.time() if now is None else now
        fresh, failures = [], []
        for provider in FAMILIES:
            try:
                value = validate_frame(self.fetcher(provider, now))
                if value["provider"] != provider or not value["coverage_from"] <= now < value["coverage_until"]:
                    raise CalendarError("返回日历类别/覆盖时间不符")
                if not -5 <= now-value["verified_at"] <= MAX_AGE_SECONDS:
                    raise CalendarError("来源核对时间过期或在未来")
                fresh.append(value)
            except Exception as exc:
                failures.append(provider)
                self._log("warning", "%s 日历刷新失败 provider=%s type=%s；有效备用仍可使用，护盘继续",
                          VERSION, provider, type(exc).__name__)
        with self._lock:
            # Keep valid prior coverage if a short weekly fallback replaces an annual feed.
            self._frames = [v for v in self._frames if now-v["verified_at"] <= MAX_AGE_SECONDS]
            for value in fresh:
                self._frames = [v for v in self._frames if not (
                    v["provider"] == value["provider"] and v["coverage_from"] == value["coverage_from"] and
                    v["coverage_until"] == value["coverage_until"])]
                self._frames.append(value)
            stored = copy.deepcopy(self._frames)
            interval=RETRY_SECONDS if failures else REFRESH_SECONDS
            if not failures:
                # A fresh short weekly feed must not wait six hours past its end.
                readiness=coverage_report(stored,now,seed=self._seed)
                ends=[instant(p["usable_until_utc"]) for p in readiness["providers"].values() if p["available_now"]]
                if ends:
                    interval=min(interval,max(60.,min(ends)-now-300.))
            self._next_refresh = time.monotonic() + interval
        if fresh:
            try:
                # Fsync must never hold the calendar lock used by entry checks
                # inside the MT5 mutex; guardian work must remain independent.
                self._persist(stored)
            except OSError as exc:
                self._log("warning", "新闻日历缓存写入失败: %s；内存有效日程保留", type(exc).__name__)
            readiness=self.coverage(now)
            self._log("info", "%s 刷新完成 provider_usable_until=%s；期限同时受覆盖范围和核对日期限制",
                      READINESS_VERSION, {p:v["usable_until_beijing"] for p,v in readiness["providers"].items()})
        return not failures

    def coverage(self, now=None, through=None):
        now=time.time() if now is None else now
        with self._lock:
            return coverage_report(self._frames,now,through,self._seed)

    def _persist(self, snapshots):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".news-calendar-", dir=self.cache_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump({"format": FORMAT, "version": VERSION, "frames": snapshots}, out, ensure_ascii=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(name, self.cache_path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def request_refresh(self):
        with self._lock:
            if not self.enabled or self.error or self._busy or time.monotonic() < self._next_refresh:
                return
            self._busy = True
        def worker():
            try:
                self.refresh()
            finally:
                with self._lock:
                    self._busy = False
        try:
            threading.Thread(target=worker, name="gold-news-calendar", daemon=True).start()
        except Exception:
            with self._lock:
                self._busy = False
                self._next_refresh = time.monotonic()+RETRY_SECONDS
            self._log("warning", "新闻日历后台线程未启动，保留已核对日程")

    def status(self, now=None, since=None):
        if not self.enabled:
            return PauseStatus(True)
        if self.error:
            return PauseStatus(False, "新闻日历配置错误: " + self.error)
        self.request_refresh()
        now = time.time() if now is None else now
        with self._lock:
            try:
                result = evaluate(self._frames+self._seed, now, since)
            except CalendarError as exc:
                result = PauseStatus(False, "新闻日历无效: " + str(exc))
            key = (result.allowed, result.reason, result.degraded)
            if key != self._last_log:
                if not result.allowed:
                    self._log("info", "%s %s；已有仓位护盘继续", VERSION, result.reason)
                elif self._last_log and not self._last_log[0]:
                    self._log("info", "%s 新闻休息/数据等待结束，重新读取行情；不执行旧提案", VERSION)
                if result.allowed and result.degraded:
                    self._log("warning", "%s 使用有期限的备用日程，后台重试取数", VERSION)
                self._last_log = key
            return result


def main():
    import argparse
    parser = argparse.ArgumentParser(description="黄金重大事件日历检查，不连接MT5、不调用AI、不下单")
    parser.add_argument("--refresh", action="store_true", help="立即刷新公开日历并写缓存")
    parser.add_argument("--through", help="检查连续覆盖到该ISO时刻（必须含时区）；不是允许期间持续开仓")
    args = parser.parse_args()
    try:
        through=instant(args.through) if args.through else None
        if through is not None and through < time.time():
            raise CalendarError("检查终点不能早于当前时刻")
    except CalendarError as exc:
        parser.error(str(exc))
    base = Path(__file__).resolve().parents[1]
    value = NewsCalendar.from_environment(base, base/"xau_fib_pa_logs")
    if args.refresh:
        value.refresh()
    # CLI check is read-only unless --refresh was explicitly supplied.
    value.request_refresh = lambda: None
    status = value.status()
    readiness=value.coverage(through=through)
    print(json.dumps({"version": VERSION, "enabled": value.enabled, "new_entry_allowed": status.allowed,
                      "reason": status.reason, "resume_beijing": local_time(status.resume_at) if status.resume_at else None,
                      "cached_fallback": status.degraded, "cache": str(value.cache_path),
                      "coverage":readiness}, ensure_ascii=False))
    return 0 if status.allowed and (not args.through or value.enabled and readiness["covers_through"]) else 2


if __name__ == "__main__":
    raise SystemExit(main())
