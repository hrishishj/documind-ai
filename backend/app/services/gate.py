import threading
import time


class SlotLimiter:
    """At most `size` concurrent holders overall (e.g. LLM-backed requests). Holds expire after `ttl` so a request that
    never reaches its `finally` (client gone before the stream starts) cannot permanently consume a slot."""

    def __init__(self, size: int, ttl: float = 600.0, clock=time.monotonic):
        self.size, self.ttl, self.clock = size, ttl, clock
        self._held: dict[int, float] = {}
        self._next = 0
        self._mu = threading.Lock()

    def acquire(self) -> int | None:
        with self._mu:
            now = self.clock()
            self._held = {k: t for k, t in self._held.items() if now - t < self.ttl}
            if len(self._held) >= self.size:
                return None
            self._next += 1
            self._held[self._next] = now
            return self._next

    def release(self, token: int | None) -> None:
        if token is not None:
            with self._mu:
                self._held.pop(token, None)


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
