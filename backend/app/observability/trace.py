"""Per-request tracing: one RunTrace per chat turn, made of spans (graph node, LLM call, external tool call).

Deliberately dependency-free and metadata-only: spans carry names, timings, token counts and error *types/messages*,
never the user's message, document text or model output, so a stored trace cannot leak document content.

The active trace travels in a ContextVar. Code that is not inside a trace (unit tests, scripts) pays nothing:
`span()` is a no-op then.
"""
import contextvars
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from app.core.config import get_settings

_current: contextvars.ContextVar["RunTrace | None"] = contextvars.ContextVar("documind_trace", default=None)
_parent: contextvars.ContextVar[str | None] = contextvars.ContextVar("documind_span_parent", default=None)

MAX_ERR = 300


@dataclass
class Span:
    id: str
    parent: str | None
    name: str
    kind: str                    # node | llm | tool
    start_ms: float              # offset from the start of the run
    duration_ms: float = 0.0
    status: str = "ok"           # ok | error
    error: str = ""
    attrs: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"id": self.id, "parent": self.parent, "name": self.name, "kind": self.kind,
                "start_ms": round(self.start_ms, 1), "duration_ms": round(self.duration_ms, 1),
                "status": self.status, "error": self.error, "attrs": self.attrs}


def cost_usd(input_tokens: int, output_tokens: int) -> float:
    s = get_settings()
    return (input_tokens * s.price_input_per_mtok + output_tokens * s.price_output_per_mtok) / 1_000_000


class RunTrace:
    def __init__(self, session_id: str = "", run_id: str | None = None):
        self.run_id = run_id or uuid.uuid4().hex
        self.session_id = session_id
        self._t0 = time.perf_counter()
        self.spans: list[Span] = []
        self._mu = threading.Lock()

    def now_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000

    def add(self, s: Span) -> None:
        with self._mu:
            self.spans.append(s)

    # ------------------------------------------------------------------ summary
    def llm_spans(self) -> list[Span]:
        return [s for s in self.spans if s.kind == "llm"]

    def totals(self) -> dict:
        llm = self.llm_spans()
        tin = sum(int(s.attrs.get("input_tokens") or 0) for s in llm)
        tout = sum(int(s.attrs.get("output_tokens") or 0) for s in llm)
        failed = [s for s in self.spans if s.status == "error"]
        return {"llm_calls": len(llm), "llm_failures": sum(1 for s in llm if s.status == "error"),
                "llm_retries": sum(1 for s in llm if int(s.attrs.get("attempt", 0)) > 0),
                "input_tokens": tin, "output_tokens": tout, "cost_usd": round(cost_usd(tin, tout), 6),
                "tool_calls": sum(1 for s in self.spans if s.kind == "tool"), "errors": len(failed)}

    def to_dict(self, duration_ms: float | None = None) -> dict:
        return {"run_id": self.run_id, "duration_ms": round(duration_ms if duration_ms is not None else self.now_ms(), 1),
                **self.totals(), "spans": [s.as_dict() for s in sorted(self.spans, key=lambda x: x.start_ms)]}


def current() -> "RunTrace | None":
    return _current.get()


@contextmanager
def use_trace(t: "RunTrace | None"):
    """Make `t` the active trace for the duration of the block (re-entered around each step of a streamed run, because
    a streamed response resumes in a fresh context each time)."""
    tok = _current.set(t)
    try:
        yield t
    finally:
        _current.reset(tok)


class _SpanHandle:
    """What `with span(...) as s` yields: lets the caller attach attributes (e.g. token counts) after the work ran."""

    def __init__(self, s: Span | None):
        self._s = s

    def set(self, **attrs) -> None:
        if self._s is not None:
            self._s.attrs.update({k: v for k, v in attrs.items() if v is not None})

    def fail(self, error: str) -> None:
        """Mark the span failed without raising (a handled failure, e.g. a node that returns an error update)."""
        if self._s is not None:
            self._s.status, self._s.error = "error", error[:MAX_ERR]


@contextmanager
def span(name: str, kind: str = "tool", **attrs):
    t = _current.get()
    if t is None:
        yield _SpanHandle(None)
        return
    s = Span(id=uuid.uuid4().hex[:12], parent=_parent.get(), name=name, kind=kind, start_ms=t.now_ms(),
             attrs={k: v for k, v in attrs.items() if v is not None})
    tok = _parent.set(s.id)
    t0 = time.perf_counter()
    try:
        yield _SpanHandle(s)
    except BaseException as e:  # noqa: BLE001 - recorded, then re-raised unchanged
        s.status, s.error = "error", f"{type(e).__name__}: {str(e)}"[:MAX_ERR]
        raise
    finally:
        s.duration_ms = (time.perf_counter() - t0) * 1000
        _parent.reset(tok)
        t.add(s)


def traced_node(name: str, fn):
    """Wrap a LangGraph node so it records a span. A node that *handles* a failure by returning an error update is
    still marked failed, so the trace shows what went wrong even though nothing was raised."""
    def wrapper(state):
        with span(name, "node") as sp:
            out = fn(state)
            if isinstance(out, dict):
                if out.get("error"):
                    sp.fail(str(out["error"]))
                if out.get("plan_errors"):
                    sp.set(plan_errors=len(out["plan_errors"]), first_plan_error=str(out["plan_errors"][0])[:160])
                if "attempts" in out:
                    sp.set(attempt=out["attempts"])
            return out
    wrapper.__name__ = f"traced_{name}"
    return wrapper
