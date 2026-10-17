"""Small, source-bound descriptions; never permissions, scores or trade gates."""
import math

VERSION = "entry-context-v1"
TIMEFRAMES = {"H1": "h1_bars", "M15": "m15_bars", "M5": "m5_bars"}
METRICS = {"available", "source_count", "first_time", "last_time", "close_change",
           "last_close_change", "recent3_close_change", "latest_vs_peak_close", "latest_vs_trough_close",
           "higher_closes", "lower_closes", "high_change", "low_change",
           "window_high", "window_low", "close_location", "buy_ask_location", "sell_bid_location"}
BACKGROUND_FIELDS = {"label", "ema_period", "ema_value", "last_closed_price", "source_count"}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _window(bars, quote):
    rows = bars[-6:] if isinstance(bars, list) else []
    if len(rows) < 2 or any(not isinstance(r, dict) or any(not number(r.get(k)) for k in
                                                        ("time", "open", "high", "low", "close")) for r in rows):
        return {"available": False, "source_count": len(rows)}
    if any(b["time"] <= a["time"] for a, b in zip(rows, rows[1:])) or any(
            r["low"] <= 0 or r["high"] < max(r["open"], r["close"]) or
            r["low"] > min(r["open"], r["close"]) for r in rows):
        return {"available": False, "source_count": len(rows)}
    low, high = min(r["low"] for r in rows), max(r["high"] for r in rows)
    def location(value):
        # Do not clamp: outside the sampled envelope is meaningful factual data.
        return round((value-low)/(high-low), 6) if number(value) and high > low else None
    return {"available": True, "source_count": len(rows), "first_time": rows[0]["time"],
            "last_time": rows[-1]["time"], "close_change": round(rows[-1]["close"]-rows[0]["close"], 8),
            "last_close_change": round(rows[-1]["close"]-rows[-2]["close"], 8),
            "recent3_close_change": round(rows[-1]["close"]-rows[-min(3,len(rows))]["close"], 8),
            "latest_vs_peak_close": round(rows[-1]["close"]-max(r["close"] for r in rows), 8),
            "latest_vs_trough_close": round(rows[-1]["close"]-min(r["close"] for r in rows), 8),
            "higher_closes": sum(b["close"] > a["close"] for a,b in zip(rows,rows[1:])),
            "lower_closes": sum(b["close"] < a["close"] for a,b in zip(rows,rows[1:])),
            "high_change": round(rows[-1]["high"]-rows[-2]["high"], 8),
            "low_change": round(rows[-1]["low"]-rows[-2]["low"], 8),
            "window_high": high, "window_low": low,
            "close_location": location(rows[-1]["close"]),
            "buy_ask_location": location(quote.get("ask")), "sell_bid_location": location(quote.get("bid"))}


def build(pack, h1_ema50=None, m15_ema50=None, h1_count=None, m15_count=None):
    quote = pack.get("live_quote", {})
    backgrounds = {}
    for tf, label, value, count in (("H1", "h1_trend", h1_ema50, h1_count),
                                  ("M15", "m15_trend", m15_ema50, m15_count)):
        bars = pack.get(TIMEFRAMES[tf], [])
        backgrounds[tf] = {"label": pack.get(label), "ema_period": 50,
                           "ema_value": value if number(value) else None,
                           "last_closed_price": bars[-1].get("close") if bars else None,
                           "source_count": count}
    return {"version": VERSION, "backgrounds": backgrounds,
            "timeframes": {tf: _window(pack.get(key), quote) for tf,key in TIMEFRAMES.items()}}


def citation_allowed(parts):
    if len(parts) == 2 and parts[1] == "version":
        return True
    if len(parts) != 4 or parts[2] not in TIMEFRAMES:
        return False
    if parts[1] == "timeframes":
        return parts[3] in METRICS
    if parts[1] == "backgrounds" and parts[2] in ("H1","M15"):
        return parts[3] in BACKGROUND_FIELDS
    return False
