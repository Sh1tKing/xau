"""JSON-safe closed-candle formatting shared by strategy definitions and AI."""
def candle_parts(row):
    body = abs(float(row["close"]) - float(row["open"]))
    full = max(float(row["high"]) - float(row["low"]), 1e-9)
    return {"body": body, "range": full,
            "upper": float(row["high"]) - max(float(row["open"]), float(row["close"])),
            "lower": min(float(row["open"]), float(row["close"])) - float(row["low"])}

def _rounded_number(value, digits=2):
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None

def _bar_payload(row, role=None):
    """Return one JSON-safe candle record with an optional evidence role."""
    if row is None:
        return None
    result = {
        "time": int(getattr(row, "time", 0) or 0),
        "open": _rounded_number(getattr(row, "open", 0.0)),
        "high": _rounded_number(getattr(row, "high", 0.0)),
        "low": _rounded_number(getattr(row, "low", 0.0)),
        "close": _rounded_number(getattr(row, "close", 0.0)),
        "tick_volume": _rounded_number(getattr(row, "tick_volume", 0.0)),
    }
    if role:
        result["role"] = role
    parts = candle_parts({key: float(getattr(row, key)) for key in ("open", "high", "low", "close")})
    result["candle_parts"] = {key: _rounded_number(parts[key], 6) for key in ("body", "range", "upper", "lower")}
    return result

def _extreme_bar(df, column, mode="min", role=None):
    if df is None or len(df) == 0 or column not in df.columns:
        return None
    try:
        index = df[column].idxmin() if mode == "min" else df[column].idxmax()
        return _bar_payload(df.loc[index], role=role)
    except (KeyError, TypeError, ValueError):
        return None

def bars_for_prompt(df, limit=8):
    if df is None or len(df) == 0:
        return []
    return [_bar_payload(row) for row in df.tail(limit).itertuples(index=False)]

def bar_times_for_prompt(df, limit):
    if df is None or len(df) == 0 or "time" not in df.columns:
        return []
    return [int(value) for value in df["time"].tail(limit).tolist()]
