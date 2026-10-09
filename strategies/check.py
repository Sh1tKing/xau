"""Validate strategy declarations without importing MT5 or sending requests."""
import json
from .framework import DEFAULT_REGISTRY, VERSION, SESSIONS


def main():
    snapshot = DEFAULT_REGISTRY.snapshot()
    if snapshot.error:
        print(snapshot.error)
        return 1
    print(json.dumps({"framework": VERSION, "fingerprint": snapshot.fingerprint,
                      "sessions": {session: [s.id for s in snapshot.active(session)] for session in SESSIONS},
                      "strategies": [s.manifest() for s in snapshot.strategies]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
