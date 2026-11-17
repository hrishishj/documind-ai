"""Process-local metrics in Prometheus text format (no client library needed). Aggregates only: no user or
document content, so the endpoint is safe to expose to a scraper."""
import threading
from collections import defaultdict

BUCKETS_S = (1, 2, 5, 10, 20, 40, 80, 160)


class Metrics:
    def __init__(self):
        self._mu = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._mu:
            self.runs: dict[tuple[str, str], int] = defaultdict(int)       # (intent, status) -> n
            self.tokens: dict[str, int] = defaultdict(int)                 # input|output -> n
            self.llm_calls = self.llm_failures = self.llm_retries = self.tool_calls = self.span_errors = 0
            self.cost_usd = 0.0
            self.lat_sum = 0.0
            self.lat_count = 0
            self.lat_buckets = [0] * len(BUCKETS_S)

    def observe_run(self, intent: str, status: str, duration_ms: float, totals: dict) -> None:
        sec = duration_ms / 1000
        with self._mu:
            self.runs[(intent or "unknown", status)] += 1
            self.tokens["input"] += totals["input_tokens"]
            self.tokens["output"] += totals["output_tokens"]
            self.llm_calls += totals["llm_calls"]
            self.llm_failures += totals["llm_failures"]
            self.llm_retries += totals["llm_retries"]
            self.tool_calls += totals["tool_calls"]
            self.span_errors += totals["errors"]
            self.cost_usd += totals["cost_usd"]
            self.lat_sum += sec
            self.lat_count += 1
            for i, b in enumerate(BUCKETS_S):
                if sec <= b:
                    self.lat_buckets[i] += 1

    def render(self) -> str:
        with self._mu:
            out = ["# TYPE documind_runs_total counter"]
            out += [f'documind_runs_total{{intent="{i}",status="{s}"}} {n}' for (i, s), n in sorted(self.runs.items())]
            out += ["# TYPE documind_tokens_total counter"]
            out += [f'documind_tokens_total{{direction="{d}"}} {n}' for d, n in sorted(self.tokens.items())]
            out += [f"# TYPE documind_llm_calls_total counter\ndocumind_llm_calls_total {self.llm_calls}",
                    f"# TYPE documind_llm_failures_total counter\ndocumind_llm_failures_total {self.llm_failures}",
                    f"# TYPE documind_llm_retries_total counter\ndocumind_llm_retries_total {self.llm_retries}",
                    f"# TYPE documind_tool_calls_total counter\ndocumind_tool_calls_total {self.tool_calls}",
                    f"# TYPE documind_span_errors_total counter\ndocumind_span_errors_total {self.span_errors}",
                    f"# TYPE documind_cost_usd_total counter\ndocumind_cost_usd_total {self.cost_usd:.6f}",
                    "# TYPE documind_run_seconds histogram"]
            out += [f'documind_run_seconds_bucket{{le="{b}"}} {n}' for b, n in zip(BUCKETS_S, self.lat_buckets)]
            out += [f'documind_run_seconds_bucket{{le="+Inf"}} {self.lat_count}',
                    f"documind_run_seconds_sum {self.lat_sum:.3f}", f"documind_run_seconds_count {self.lat_count}"]
        return "\n".join(out) + "\n"
