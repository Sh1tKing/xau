"""Durable account-wide ambiguity latch; absence of a position is not rejection."""
import json
import os
from pathlib import Path


def shared_account_state(identity):
    """Same durable user/account path for every deployed folder copy."""
    root = Path(os.environ["LOCALAPPDATA"]) if os.environ.get("LOCALAPPDATA") else Path.home()/".local"/"state"
    return root/"MYQCryptoDemo"/identity


class PendingOrder:
    def __init__(self, path, identity):
        self.path, self.identity = Path(path), identity

    def read(self):
        if not self.path.exists():
            return None
        with self.path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if value.get("account") != self.identity:
            raise RuntimeError("pending account identity mismatch")
        return value

    def reserve(self, request):
        if self.read() is not None:
            raise RuntimeError("unresolved account-wide order receipt")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        value = dict(account=self.identity, token=request["comment"], symbol=request["symbol"],
                     magic=request["magic"], action=request["type"])
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(value, stream, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def clear(self):
        self.path.unlink(missing_ok=True)

    def unresolved(self, positions):
        value = self.read()
        if value is None:
            return False
        if positions is None:
            return True
        matches = [p for p in positions if getattr(p, "comment", None) == value["token"]
                   and getattr(p, "symbol", None) == value["symbol"]
                   and getattr(p, "magic", None) == value["magic"]
                   and getattr(p, "type", None) == value["action"]]
        if len(matches) == 1:
            self.clear()
            return False
        return True
