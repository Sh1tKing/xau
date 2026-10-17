"""Process-local read-timeout backoff; no HTTP, credentials, disk or trading."""
from dataclasses import dataclass
import threading
import time

VERSION = "ai1-primary-backoff-v1"


@dataclass(frozen=True)
class Lease:
    key: str | None = None
    epoch: int = 0
    probe: object = None
    bypass: bool = False
    remaining: float = 0.0


class PrimaryTimeoutBackoff:
    """After consecutive read timeouts, use an available independent backup.

    Recovery uses one ordinary scheduled request, never an extra paid probe.
    A lease released before POST does not count as a timeout or hold a probe.
    """
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._lock = threading.RLock()
        self._states = {}

    def enter(self, key, threshold, cooldown, backup_available):
        if not backup_available or threshold <= 0 or cooldown <= 0:
            return Lease()
        with self._lock:
            state = self._states.setdefault(key, {"failures": 0, "until": 0., "epoch": 0, "probe": None})
            now = self.clock()
            if state["until"] > now or state["probe"] is not None:
                return Lease(key, state["epoch"], bypass=True, remaining=max(0., state["until"]-now))
            probe = object() if state["failures"] >= threshold else None
            state["probe"] = probe
            return Lease(key, state["epoch"], probe)

    def read_timeout(self, lease, threshold, cooldown):
        if lease.key is None or lease.bypass:
            return
        with self._lock:
            state = self._states.get(lease.key)
            if state is None or state["epoch"] != lease.epoch:
                return
            state["failures"] += 1
            if state["failures"] >= threshold:
                state["until"] = self.clock() + cooldown
                state["epoch"] += 1
                state["probe"] = None

    def response_received(self, lease):
        # This measures transport health only, not JSON or trading validity.
        if lease.key is None or lease.bypass:
            return lease
        with self._lock:
            state = self._states.get(lease.key)
            if state is not None and state["epoch"] == lease.epoch:
                state.update(failures=0, until=0., epoch=state["epoch"]+1, probe=None)
                return Lease(lease.key, state["epoch"])
            return Lease()

    def release(self, lease):
        with self._lock:
            state = self._states.get(lease.key)
            if state is not None and lease.probe is not None and state["probe"] is lease.probe:
                state["probe"] = None
