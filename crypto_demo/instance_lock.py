"""Account-wide OS byte lock shared by BTC and ETH processes, released on crash."""
import hashlib
import os
from pathlib import Path
import tempfile


def account_id(login, server):
    if not str(login).strip() or not str(server).strip():
        raise ValueError("account login/server required")
    return hashlib.sha256((str(login)+"\0"+str(server)).encode()).hexdigest()[:24]


class AccountLock:
    def __init__(self, login, server, directory=None):
        self.identity = account_id(login, server)
        self.path = Path(directory or Path(tempfile.gettempdir())/"myq-crypto-demo-locks") / (self.identity+".lock")
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = open(self.path, "a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise RuntimeError("another crypto instance holds this account") from exc
        return self

    def close(self):
        if self.file:
            self.file.close()
            self.file = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *args):
        self.close()
