"""Only operational entry; offline options never import/initialize MT5."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE.parent))


def configure(asset, mode, symbol=None, offline=False):
    for key in list(os.environ):
        if key.startswith(("FIB_PA_", "MT5_", "AI1_", "AI2_", "AI3_", "GPTSTORE_", "FEISHU_", "CRYPTO_")):
            os.environ.pop(key, None)
    if not offline and (BASE/".env").exists():
        for raw in (BASE/".env").read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key.strip()] = value.strip().strip('"').strip("'")
    os.environ.update(CRYPTO_ASSET=asset, CRYPTO_MODE=mode, CRYPTO_RUN_AUTHORIZED="1")
    exact = symbol or os.environ.get("CRYPTO_"+asset+"_SYMBOL", "")
    os.environ["FIB_PA_SYMBOL"] = exact
    os.environ["FIB_PA_ENABLE_TRADING"] = "true" if mode == "demo" else "false"
    os.environ["FIB_PA_AI_ENABLE_TRADING"] = os.environ["FIB_PA_ENABLE_TRADING"]
    os.environ["FIB_PA_AI_ENABLED"] = "false" if mode == "observe" else "true"
    os.environ.setdefault("FEISHU_NOTIFY_ENABLED", "false")
    return exact


def main():
    parser = argparse.ArgumentParser(description="Independent BTC/ETH MT5 CFD DEMO fork; default observe, no AI or orders.")
    parser.add_argument("--asset", choices=("BTC", "ETH"), default="BTC")
    parser.add_argument("--mode", choices=("observe", "dry-run", "demo"), default="observe")
    parser.add_argument("--symbol", help="exact broker symbol including suffix; never guessed")
    parser.add_argument("--check", action="store_true", help="offline definitions check; no MT5")
    parser.add_argument("--self-test", action="store_true", help="offline mock tests; no credentials/terminal")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    configure(args.asset, args.mode, args.symbol, offline=args.check or args.self_test)
    if args.check:
        from crypto_demo.strategies.framework import DEFAULT_REGISTRY
        snapshot = DEFAULT_REGISTRY.snapshot()
        if snapshot.error:
            parser.error(snapshot.error)
        print(json.dumps(dict(asset=args.asset, mode=args.mode, fingerprint=snapshot.fingerprint,
                              strategies=[s.manifest() for s in snapshot.strategies]), ensure_ascii=False))
        return 0
    if args.self_test:
        return subprocess.call([sys.executable, "-m", "unittest", "discover", "-s", str(BASE/"tests"), "-v"], cwd=BASE.parent)
    if not os.environ["FIB_PA_SYMBOL"]:
        parser.error("configure exact CRYPTO_"+args.asset+"_SYMBOL in crypto_demo/.env or pass --symbol")
    for key in ("MT5_LOGIN", "MT5_PASSWORD", "MT5_SERVER"):
        if not os.environ.get(key):
            parser.error("missing crypto_demo/.env field: "+key)
    from crypto_demo import crypto_ai3
    if args.mode != "observe" and not all((crypto_ai3.AI1_KEY, crypto_ai3.AI2_KEY, crypto_ai3.AI3_KEY,
            crypto_ai3.AI1_URL, crypto_ai3.AI2_URL, crypto_ai3.AI3_URL,
            crypto_ai3.AI1_MODEL, crypto_ai3.AI2_MODEL, crypto_ai3.AI3_MODEL)):
        parser.error("dry-run/demo require three configured AI endpoints, models and credentials")
    return crypto_ai3.ThreeAITrader().run(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
