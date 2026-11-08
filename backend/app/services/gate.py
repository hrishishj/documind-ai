import threading
import time


class SessionGate:
    """At most one running request per session. A hold older than `ttl` is treated as abandoned, so a client that
    disconnects before its stream starts (when a generator's `finally` never runs) cannot lock a session forever."""

    def __init__(self, ttl: float = 600.0, clock=time.monotonic):
        self._held: dict[str, float] = {}
        self._mu = threading.Lock()
        self.ttl, self.clock = ttl, clock

    def acquire(self, key: str) -> bool:
        with self._mu:
            now = self.clock()
            t = self._held.get(key)
            if t is not None and now - t < self.ttl:
                return False
            self._held[key] = now
            return True

    def release(self, key: str) -> None:
        with self._mu:
            self._held.pop(key, None)
