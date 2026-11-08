"""Web search behind one interface: Tavily first, a multi-engine meta-search (ddgs) as fallback.

The server never fetches result pages itself (that would be an SSRF risk: a hostile page could redirect it to internal
addresses). Tavily already returns extracted page content; the fallback only has search-result snippets, which is a
documented quality trade-off.
"""
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

log = logging.getLogger(__name__)

MAX_QUERY = 300
MAX_TEXT = 1200
LOW_QUALITY_DOMAINS = {"facebook.com", "instagram.com", "tiktok.com", "pinterest.com", "x.com", "twitter.com", "youtube.com",
                       "reddit.com", "quora.com", "linkedin.com"}


@dataclass
class WebResult:
    title: str
    url: str
    text: str               # extracted content / snippet, cleaned and length-capped
    score: float = 0.0      # engine-provided relevance in [0, 1] when available
    engine: str = ""

    @property
    def domain(self) -> str:
        return domain_of(self.url)


class SearchError(Exception):
    """Every configured engine failed or returned nothing."""


class WebSearch(Protocol):
    def search(self, query: str, max_results: int = 5) -> list[WebResult]: ...


def domain_of(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def clean_text(s: str, limit: int = MAX_TEXT) -> str:
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f\u200b\u202f\u00a0]+", " ", s or "")
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def clean_query(q: str) -> str:
    return clean_text(q, MAX_QUERY)


def safe_url(url: str) -> str | None:
    """Only plain http(s) links are ever shown to or clicked by users (never javascript:, data:, file: ...)."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname:
        return None
    return p.geturl()


def is_low_quality(url: str) -> bool:
    d = domain_of(url)
    return any(d == b or d.endswith("." + b) for b in LOW_QUALITY_DOMAINS)


def normalise(results: list[WebResult], max_results: int) -> list[WebResult]:
    """Drop unsafe/empty/low-quality results and duplicates (same URL, or a page already seen on the same domain+title)."""
    out, seen = [], set()
    for r in results:
        url = safe_url(r.url)
        text = clean_text(r.text)
        if not url or len(text) < 40 or is_low_quality(url):
            continue
        key = (url.split("#")[0].rstrip("/"), clean_text(r.title, 80).lower())
        if key[0] in {k[0] for k in seen} or key in seen:
            continue
        seen.add(key)
        out.append(WebResult(clean_text(r.title, 160) or domain_of(url), url, text, r.score, r.engine))
        if len(out) >= max_results:
            break
    return out


class TavilySearch:
    def __init__(self, api_key: str, depth: str = "basic"):
        from tavily import TavilyClient

        self._c = TavilyClient(api_key=api_key)
        self.depth = depth

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        r = self._c.search(clean_query(query), max_results=max_results + 3, search_depth=self.depth)  # extra: some are filtered out
        raw = [WebResult(x.get("title", ""), x.get("url", ""), x.get("content", ""), float(x.get("score") or 0), "tavily")
               for x in r.get("results", [])]
        return normalise(raw, max_results)


class MetaSearch:
    """ddgs meta-search. DuckDuckGo's own backend often returns nothing, so several engines are tried in turn."""

    ENGINES = ("auto", "bing", "google")

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        from ddgs import DDGS

        last: Exception | None = None
        for engine in self.ENGINES:
            try:
                rows = DDGS(timeout=12).text(clean_query(query), max_results=max_results + 3, backend=engine)
            except Exception as e:  # noqa: BLE001 - "No results found" and rate limits both mean "try the next engine"
                last = e
                continue
            res = normalise([WebResult(x.get("title", ""), x.get("href", ""), x.get("body", ""), 0.0, f"ddgs:{engine}") for x in rows],
                            max_results)
            if res:
                return res
        raise SearchError(f"meta-search found nothing ({type(last).__name__ if last else 'empty'})")


class FallbackSearch:
    """Try engines in order; the first non-empty answer wins. Errors are collected so the caller can explain."""

    def __init__(self, engines: list):
        self.engines = engines

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        errors = []
        for e in self.engines:
            try:
                res = e.search(query, max_results)
                if res:
                    return res
                errors.append(f"{type(e).__name__}: no usable results")
            except Exception as ex:  # noqa: BLE001
                log.warning("%s failed: %s", type(e).__name__, str(ex)[:200])
                errors.append(f"{type(e).__name__}: {str(ex)[:120]}")
        raise SearchError("; ".join(errors) or "no search engine configured")


class CachedSearch:
    """Identical queries within `ttl` seconds reuse the previous results (saves Tavily credits and time)."""

    def __init__(self, inner: WebSearch, ttl: float = 600.0, maxsize: int = 128, clock=time.monotonic):
        self.inner, self.ttl, self.maxsize, self.clock = inner, ttl, maxsize, clock
        self._d: dict[tuple, tuple[float, list[WebResult]]] = {}
        self._mu = threading.Lock()

    def search(self, query: str, max_results: int = 5) -> list[WebResult]:
        key = (clean_query(query).lower(), max_results)
        now = self.clock()
        with self._mu:
            hit = self._d.get(key)
            if hit and now - hit[0] < self.ttl:
                return list(hit[1])
        res = self.inner.search(query, max_results)  # failures are never cached
        with self._mu:
            if len(self._d) >= self.maxsize:
                self._d.pop(min(self._d, key=lambda k: self._d[k][0]))
            self._d[key] = (now, res)
        return list(res)


def build_search(tavily_key: str = "") -> WebSearch:
    engines: list = []
    if tavily_key:
        engines.append(TavilySearch(tavily_key))
    engines.append(MetaSearch())
    return CachedSearch(FallbackSearch(engines))
