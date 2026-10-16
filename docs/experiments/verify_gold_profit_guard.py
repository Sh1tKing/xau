"""Offline gold verification: forbid unmocked MT5/HTTP and isolate all state writes."""
import argparse
import fnmatch
import hashlib
import io
import json
import logging
import os
from pathlib import Path
import sys
import socket
import tempfile
import unittest
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--pattern", default="test_*.py")
    parser.add_argument("--self-tests-only", action="store_true")
    parser.add_argument("--verbosity", type=int, choices=(0,1,2), default=1)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    root = Path(args.root).resolve()
    sys.path.insert(0, str(root))
    def digest():
        watched = [root/".env",root/"server_deploy"/".env"]
        for area in (root/"xau_fib_pa_logs", root/"server_deploy"/"xau_fib_pa_logs"):
            if area.exists():
                watched.extend(p for p in area.glob("*.json") if "state" in p.name or "context" in p.name)
        return {str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in watched if p.is_file()}
    before = digest()
    with tempfile.TemporaryDirectory(prefix="gold-profit-offline-") as temp:
        env = {"FIB_PA_GUARDIAN_STATE_FILE":str(Path(temp)/"guard.json"),
               "FIB_PA_STRATEGY_STATE_FILE":str(Path(temp)/"events.json"),
               "FIB_PA_AI3_STATE_FILE":str(Path(temp)/"entry.json"),
               "FIB_PA_NEWS_CALENDAR_CACHE":str(Path(temp)/"news.json"),
               # Historical synthetic bars have their own clock. Dedicated
               # news tests inject enabled, dated schedules and HTTP failures.
               "FIB_PA_NEWS_PAUSE_ENABLED":"false"}
        with patch.dict(os.environ, env):
            original_log_open = logging.FileHandler._open
            def isolated_log_open(handler):
                target = Path(handler.baseFilename).resolve()
                return io.StringIO() if target.is_relative_to(root/"xau_fib_pa_logs") else original_log_open(handler)
            patches_log = patch.object(logging.FileHandler,"_open",isolated_log_open)
            patches_log.start()
            import MetaTrader5 as mt5
            import requests
            import xauusd_fib_volume_pa_ai3 as ai
            patches_state = patch.object(ai,"AI3_STATE_FILE",str(Path(temp)/"entry.json"))
            patches_state.start()
            patches_snapshots = patch.object(ai,"AI_DECISION_SNAPSHOT_DIR",str(Path(temp)/"ai_decisions"))
            patches_usage = patch.object(ai,"AI_USAGE_DIR",str(Path(temp)/"ai_usage"))
            patches_snapshots.start()
            patches_usage.start()
            original_store_init = ai.base.GuardStore.__init__
            store_number = 0
            def isolated_store(store,path):
                nonlocal store_number
                resolved = Path(path).resolve()
                if resolved == root/"xau_fib_pa_logs"/"guardian_profit_state.json":
                    store_number += 1
                    path = Path(temp)/("guard-default-%s.json" % store_number)
                original_store_init(store,path)
            patches_store = patch.object(ai.base.GuardStore,"__init__",isolated_store)
            patches_store.start()
            def forbidden(*a, **kw):
                raise AssertionError("unmocked terminal/HTTP call is forbidden in offline verification")
            patches = [patch.object(mt5,name,side_effect=forbidden) for name in ("initialize","login","order_send")]
            # Uninitialized reads historically returned None in the old tests.
            # Stub them explicitly so no terminal access occurs at all.
            patches += [patch.object(mt5,name,return_value=None) for name in (
                "shutdown","account_info","positions_get","symbol_info","symbol_info_tick",
                "copy_rates_from_pos","history_deals_get","order_calc_profit")]
            patches += [patch.object(requests,"post",side_effect=forbidden)]
            patches += [patch.object(requests.sessions.Session,"request",side_effect=forbidden),
                        patch.object(socket.socket,"connect",side_effect=forbidden),
                        patch.object(socket.socket,"connect_ex",side_effect=forbidden)]
            for scope in patches:scope.start()
            try:
                if args.self_tests_only:
                    ai.base.run_self_test()
                    ai.run_self_test()
                    result = unittest.TestResult()
                else:
                    manifest = root/"MANIFEST.json"
                    if manifest.is_file():
                        # Deployment may retain unrelated legacy files. Verify
                        # only this gold delivery's declared modules, never delete them.
                        declared = json.loads(manifest.read_text(encoding="utf-8"))["files_sha256"]
                        paths = [Path(name) for name in declared if Path(name).parent == Path("tests")
                                 and fnmatch.fnmatch(Path(name).name,args.pattern)]
                        sys.path.insert(0,str(root/"tests"))
                        suite = unittest.TestSuite()
                        for path in sorted(paths):
                            suite.addTests(unittest.defaultTestLoader.loadTestsFromName(path.stem))
                    else:
                        suite = unittest.defaultTestLoader.discover(str(root/"tests"),pattern=args.pattern)
                    result = unittest.TextTestRunner(verbosity=args.verbosity).run(suite)
            finally:
                for scope in reversed(patches):scope.stop()
                patches_store.stop()
                patches_usage.stop()
                patches_snapshots.stop()
                patches_state.stop()
                patches_log.stop()
    unchanged = before == digest()
    print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
                          self_tests="PASS x2" if args.self_tests_only else None,
                          configuration_and_existing_state_unchanged=unchanged),ensure_ascii=False))
    return 0 if result.wasSuccessful() and unchanged else 1


if __name__ == "__main__":
    raise SystemExit(main())
