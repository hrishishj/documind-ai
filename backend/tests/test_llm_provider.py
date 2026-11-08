import pytest

from app.core.errors import ExternalServiceError
from app.llm.provider import GroqLLM, TokenGovernor, _is_retryable, _retry_after, est_tokens


class FakeClock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def gov(limit=1000):
    c = FakeClock()
    return TokenGovernor(limit=limit, window=60, clock=c.now, sleep=c.sleep), c


def test_governor_allows_under_limit_without_waiting():
    g, c = gov()
    assert g.acquire(400) == 0 and g.acquire(500) == 0 and c.slept == []


def test_governor_waits_for_the_window_to_free_up():
    g, c = gov()
    g.acquire(900)
    c.t = 10.0
    waited = g.acquire(500)  # 900 used until t=60 -> must wait ~50s
    assert 49 < waited < 52 and c.t >= 60


def test_governor_never_deadlocks_on_an_oversized_request():
    g, c = gov(limit=1000)
    assert g.acquire(5000) == 0  # larger than the whole budget: pass through, server decides


def test_governor_window_slides():
    g, c = gov()
    g.acquire(1000)
    c.t = 61
    assert g.acquire(1000) == 0


def test_settle_replaces_the_reservation_with_actual_usage():
    g, c = gov(limit=1000)
    g.acquire(900)            # pessimistic reservation (prompt + max_tokens)
    g.settle(900, 150)        # the call really used 150
    assert g.acquire(800) == 0 and c.slept == []  # no stall: 150 + 800 fits


def test_without_settling_the_same_sequence_would_stall():
    g, c = gov(limit=1000)
    g.acquire(900)
    assert g.acquire(800) > 0  # documents why settle() matters (this was the 53 s chat delay)


def test_call_settles_using_usage_metadata(monkeypatch):
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda s: None)
    llm = GroqLLM.__new__(GroqLLM)
    llm.retries, llm.governor, llm._messages = 1, TokenGovernor(limit=1000), GroqLLM._messages

    class Msg:
        usage_metadata = {"total_tokens": 120}

    class R:
        def invoke(self, m):
            return {"raw": Msg(), "parsed": object(), "parsing_error": None}

    llm._call(lambda: R(), "s" * 100, "u", None, 800)  # reserves ~ 800 + prompt
    assert llm.governor._used(llm.governor.clock()) == 120


def test_unparseable_structured_output_raises_a_retryable_error():
    from app.llm.provider import StructuredOutputError
    llm = GroqLLM.__new__(GroqLLM)
    llm.retries, llm.governor, llm._messages, llm.method = 0, TokenGovernor(limit=10**9), GroqLLM._messages, None

    class Chat:
        def __init__(self, **kw): ...
        def with_structured_output(self, schema, **kw):
            class R:
                def invoke(self, m):
                    return {"raw": None, "parsed": None, "parsing_error": ValueError("bad json")}
            return R()
    llm._ChatGroq, llm._kw = Chat, {}
    with pytest.raises(StructuredOutputError, match="bad json"):
        llm.structured(dict, "s", "u")
    assert issubclass(StructuredOutputError, ExternalServiceError)


def test_est_tokens_is_a_modest_overestimate():
    assert 280 < est_tokens("a" * 1000) < 400


class Err(Exception):
    pass


def test_retry_after_parsing():
    assert _retry_after(Err("Rate limit ... Please try again in 3.45s. Need more")) == 3.45
    class R: headers = {"retry-after": "7"}
    e = Err("x"); e.response = R()
    assert _retry_after(e) == 7.0
    assert _retry_after(Err("nothing")) is None


def test_retryable_classification():
    class RateLimitError(Exception): ...
    assert _is_retryable(RateLimitError("x")) and _is_retryable(Err("HTTP 429 too many")) and not _is_retryable(Err("bad request"))


def test_call_retries_then_succeeds_and_wraps_final_failure(monkeypatch):
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda s: None)
    llm = GroqLLM.__new__(GroqLLM)  # skip the network client; exercise only the retry loop
    llm.retries, llm.governor = 3, TokenGovernor(limit=10**9)
    llm._messages = GroqLLM._messages
    calls = {"n": 0}

    class Runner:
        def invoke(self, msgs):
            calls["n"] += 1
            if calls["n"] < 3:
                raise Err("429 rate limit, try again in 0.1s")
            return "ok"

    assert llm._call(lambda: Runner(), "s", "u", None, 100) == "ok" and calls["n"] == 3

    class Bad:
        def invoke(self, msgs):
            raise ValueError("schema mismatch")
    with pytest.raises(ExternalServiceError):
        llm._call(lambda: Bad(), "s", "u", None, 100)  # non-retryable: fails immediately, wrapped


def test_call_gives_up_after_all_retries(monkeypatch):
    monkeypatch.setattr("app.llm.provider.time.sleep", lambda s: None)
    llm = GroqLLM.__new__(GroqLLM)
    llm.retries, llm.governor, llm._messages = 2, TokenGovernor(limit=10**9), GroqLLM._messages
    n = {"c": 0}

    class Always:
        def invoke(self, m):
            n["c"] += 1
            raise Err("429 rate limit")
    with pytest.raises(ExternalServiceError):
        llm._call(lambda: Always(), "s", "u", None, 50)
    assert n["c"] == 3  # 1 try + 2 retries
