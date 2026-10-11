"""Candle geometry facts only; no trading instruction or trend-first inference."""
from crypto_demo.strategies.regions import _rows, _continuous


def _color(bar):
    return 1 if bar['close'] > bar['open'] else -1 if bar['close'] < bar['open'] else 0


def _contains(current, previous, body=True):
    a, b = sorted((current['open'], current['close'])) if body else (current['low'], current['high'])
    c, d = sorted((previous['open'], previous['close'])) if body else (previous['low'], previous['high'])
    return a <= c and b >= d and (a < c or b > d)


def patterns_facts(df, timeframe, regions=None):
    """Input must be closed bars. Without decision clock no closedness is inferred.

    Caller owns closed-frame selection; this API describes geometry only.
    """
    out = {'available': False, 'timeframe': timeframe, 'blockers': [], 'coverage_start': None}
    try:
        if timeframe not in ('M5', 'M15'):
            raise ValueError('unsupported timeframe')
        rows = _rows(df, timeframe)
        step = 300 if timeframe == 'M5' else 900
        relevant = rows[-14:]
        if len(rows) < 2 or not _continuous(relevant, step):
            raise ValueError('missing/gapped pattern coverage')
        current, previous = rows[-1], rows[-2]
        opposite = _color(current)*_color(previous) == -1
        body = opposite and _contains(current, previous)
        wick = _contains(current, previous, False)
        count, covered = 0, []
        if _color(current):
            for bar in reversed(rows[-6:-1]):
                if _color(bar) != -_color(current) or not _contains(current, bar):
                    break
                count += 1
                covered.append(bar)
        prior = []
        for i in range(max(1, len(rows)-9), len(rows)-1):
            if _color(rows[i])*_color(rows[i-1]) == -1 and _contains(rows[i], rows[i-1]):
                prior.append(rows[i]['time'])
        out.update(available=True, coverage_start=relevant[0]['time'], bar_time=current['time'],
            body_engulf_previous=body, full_range_engulf_previous=wick,
            current_doji=_color(current)==0, previous_doji=_color(previous)==0,
            equal_body=sorted((current['open'], current['close'])) == sorted((previous['open'], previous['close'])),
            equal_range=(current['low'], current['high']) == (previous['low'], previous['high']),
            contiguous_opposite_bodies_covered=count, covered_bars=covered,
            source_bars=[previous, current], past8_same_pattern_times=prior,
            prior_in_covered_window=bool(prior), first_in_trend='unknown')
        if regions is not None:
            out['region_facts'] = [{'region_id': r['region_id'], 'close_distance': current['close']-r['center'],
                'range_intersects': current['low'] <= r['upper'] and current['high'] >= r['lower'],
                'close_above_upper': current['close'] > r['upper'], 'close_below_lower': current['close'] < r['lower']}
                for r in regions]
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        out['blockers'].append(str(exc))
    return out
