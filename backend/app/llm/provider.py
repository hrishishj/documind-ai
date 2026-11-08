"""LLM access behind one seam so the rest of the app (and tests) never touch a vendor SDK directly.

Groq's free tier allows 8,000 tokens/minute per model, and the limit is charged on prompt + the reserved
`max_tokens`, so this module (a) reserves only what each call type needs, (b) paces requests with a sliding-window
governor instead of discovering the limit through 429s, and (c) obeys the server's retry-after if a 429 still happens.
"""
import logging
import random
import re
import threading
import time
from collections import deque
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.core.config import get_settings
from app.core.errors import ExternalServiceError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

class StructuredOutputError(ExternalServiceError):
    """The model replied, but not with valid structured output. Callers should retry with feedback, not give up."""

    code = "structured_output_error"


STRUCTURED_MAX_TOKENS = 1500   # a plan/route is small JSON; reserving 4k+ would waste the per-minute budget
TEXT_MAX_TOKENS = 900


class LLM(Protocol):
    def structured(self, schema: type[T], system: str, user: str, history: list[tuple[str, str]] | None = None,
                   max_tokens: int | None = None) -> T: ...

    def text(self, system: str, user: str, history: list[tuple[str, str]] | None = None,
             max_tokens: int | None = None) -> str: ...


def est_tokens(*parts: str) -> int:
    """Rough prompt size (≈3.3 chars/token for English + JSON). Deliberately a slight over-estimate."""
    return int(sum(len(p) for p in parts) / 3.3) + 50


class TokenGovernor:
    """Sliding 60 s window: block until `need` more tokens fit under `limit`."""

    def __init__(self, limit: int = 7200, window: float = 60.0, clock=time.monotonic, sleep=time.sleep):
        self.limit, self.window, self.clock, self.sleep = limit, window, clock, sleep
        self._events: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def _used(self, now: float) -> int:
        while self._events and now - self._events[0][0] >= self.window:
            self._events.popleft()
        return sum(n for _, n in self._events)

    def settle(self, reserved: int, actual: int) -> None:
        """Replace a reservation with what the call really consumed (the server charges actual usage once a
        call finishes, so keeping the pessimistic reservation would stall the next request for nothing)."""
        with self._lock:
            for i in range(len(self._events) - 1, -1, -1):
                t, n = self._events[i]
                if n == min(reserved, self.limit):
                    self._events[i] = (t, max(0, actual))
                    return

    def acquire(self, need: int) -> float:
        """Reserve `need` tokens; returns seconds waited. A single request larger than the limit is let through
        (the server will decide) rather than deadlocking."""
        waited = 0.0
        while True:
            with self._lock:
                now = self.clock()
                if need >= self.limit or self._used(now) + need <= self.limit:
                    self._events.append((now, min(need, self.limit)))
                    return waited
                wait = max(0.05, self.window - (now - self._events[0][0]) + 0.05)
            log.info("token governor: waiting %.1fs", wait)
            self.sleep(wait)
            waited += wait


def _retry_after(e: Exception) -> float | None:
    resp = getattr(e, "response", None)
    ra = getattr(resp, "headers", {}).get("retry-after") if resp is not None else None
    if ra:
        try:
            return float(ra)
        except ValueError:
            pass
    m = re.search(r"try again in ([0-9.]+)s", str(e))
    return float(m.group(1)) if m else None


def _is_retryable(e: Exception) -> bool:
    name, msg = type(e).__name__, str(e).lower()
    return name in ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError") or \
        "rate limit" in msg or "429" in msg or "503" in msg


class GroqLLM:
    def __init__(self, model: str | None = None, temperature: float = 0.0, retries: int = 4, timeout: float = 60.0,
                 reasoning_effort: str | None = "low", method: str | None = None, governor: TokenGovernor | None = None):
        from langchain_groq import ChatGroq

        s = get_settings()
        if not s.groq_api_key:
            raise ExternalServiceError("GROQ_API_KEY is not configured")
        self._kw = dict(model=model or s.groq_model, api_key=s.groq_api_key, temperature=temperature, timeout=timeout, max_retries=0)
        if reasoning_effort:
            self._kw["reasoning_effort"] = reasoning_effort
        self._ChatGroq = ChatGroq
        self.retries, self.method = retries, method
        self.governor = governor or TokenGovernor()

    @staticmethod
    def _messages(system, user, history):
        return [("system", system), *(history or []), ("human", user)]

    @staticmethod
    def _actual_tokens(msg) -> int | None:
        um = getattr(msg, "usage_metadata", None) or {}
        return um.get("total_tokens")

    def _call(self, make_runner, system, user, history, max_tokens):
        need = est_tokens(system, user, *[h[1] for h in (history or [])]) + max_tokens
        for attempt in range(self.retries + 1):
            self.governor.acquire(need)
            try:
                result = make_runner().invoke(self._messages(system, user, history))
                raw = result.get("raw") if isinstance(result, dict) else result
                actual = self._actual_tokens(raw)
                if actual is not None:
                    self.governor.settle(need, actual)
                return result
            except Exception as e:  # noqa: BLE001 - classified below
                if attempt == self.retries or not _is_retryable(e):
                    raise ExternalServiceError(f"LLM call failed: {type(e).__name__}: {str(e)[:200]}") from e
                wait = _retry_after(e)
                wait = min(60.0, (wait + 0.5) if wait else 2 ** attempt + random.random())
                log.warning("LLM retry %d after %s (sleep %.1fs)", attempt + 1, type(e).__name__, wait)
                time.sleep(wait)

    def structured(self, schema, system, user, history=None, max_tokens=None):
        mt = max_tokens or STRUCTURED_MAX_TOKENS
        kw = {"method": self.method} if self.method else {}
        res = self._call(lambda: self._ChatGroq(max_tokens=mt, **self._kw).with_structured_output(schema, include_raw=True, **kw),
                         system, user, history, mt)
        if res.get("parsed") is None:  # the model answered but not in the schema: a retryable *content* problem
            raise StructuredOutputError(f"the model's output did not match the schema: {str(res.get('parsing_error'))[:300]}")
        return res["parsed"]

    def text(self, system, user, history=None, max_tokens=None):
        mt = max_tokens or TEXT_MAX_TOKENS
        return self._call(lambda: self._ChatGroq(max_tokens=mt, **self._kw), system, user, history, mt).content


def get_llm() -> LLM:
    return GroqLLM()
