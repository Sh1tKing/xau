"""Research CSV interface only; inherited gold CFD cash replay is disabled."""
import argparse
import csv
from pathlib import Path


def read_csv(path):
    with Path(path).open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or any(key not in rows[0] for key in ("time", "open", "high", "low", "close")):
        raise ValueError("CSV requires time/open/high/low/close")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Research CSV interface; CFD replay disabled without contract cash/margin/tick data. No AI/profit results.")
    parser.add_argument("--csv", help="inspect research OHLC only; no trades or cash P/L")
    args = parser.parse_args()
    if not args.csv:
        parser.error("Inherited gold replay disabled; provide --csv for research inspection")
    print("Research rows:", len(read_csv(args.csv)), "; CFD/AI profit not calculated")


if __name__ == "__main__":
    main()
