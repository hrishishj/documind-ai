import pytest

from app.core.config import get_settings
from app.research.search import (CachedSearch, FallbackSearch, MetaSearch, SearchError, TavilySearch, WebResult, build_search,
                                 clean_query, clean_text, domain_of, is_low_quality, normalise, safe_url)

LONG = "A reasonably long snippet of text that is clearly more than forty characters in length."


def R(url, title="Title", text=LONG, score=0.5):
    return WebResult(title, url, text, score, "t")


def test_only_plain_http_links_are_ever_exposed():
    assert safe_url("https://example.com/a?b=1#c") == "https://example.com/a?b=1#c"
    for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "ftp://x.org/f", "//evil.com", "", "https://", "not a url"):
        assert safe_url(bad) is None, bad


def test_domain_extraction_and_low_quality_filter():
    assert domain_of("https://www.Example.com/x") == "example.com" and domain_of("http://sub.site.org") == "sub.site.org"
    assert is_low_quality("https://www.facebook.com/p/1") and is_low_quality("https://m.youtube.com/watch") and is_low_quality("https://x.com/a")
    assert not is_low_quality("https://notfacebook.com/") and not is_low_quality("https://wikipedia.org/")


def test_clean_text_strips_control_and_odd_whitespace_and_caps_length():
    # control / zero-width / odd-space characters become a plain space (never glue two words together)
    assert clean_text("a\x00b\x07 c\u202fd\u200be   f\n\ng") == "a b c d e f g"
    out = clean_text("x" * 5000, 100)
    assert len(out) == 100 and out.endswith("…")
    assert len(clean_query("q" * 1000)) <= 300


def test_normalise_filters_dedupes_and_limits():
    res = normalise([R("https://a.com/1"), R("https://a.com/1/"), R("https://facebook.com/p"), R("javascript:alert(1)"),
                     R("https://b.com/", text="too short"), R("https://c.com/"), R("https://d.com/"), R("https://e.com/")], 3)
    assert [r.url for r in res] == ["https://a.com/1", "https://c.com/", "https://d.com/"]
    assert res[0].domain == "a.com"


def test_normalise_cleans_fields():
    r = normalise([R("https://a.com/", title="  Spaced \x00 Title ", text="Text\x00 with   noise " + LONG)], 5)[0]
    assert r.title == "Spaced Title" and "\x00" not in r.text and "  " not in r.text


class Fake:
    def __init__(self, results=None, error=None):
        self.results, self.error, self.calls = results, error, 0

    def search(self, query, max_results=5):
        self.calls += 1
        if self.error:
            raise self.error
        return self.results


def test_fallback_uses_the_next_engine_when_the_first_fails_or_is_empty():
    ok = Fake([R("https://a.com/")])
    assert FallbackSearch([Fake(error=RuntimeError("tavily down")), ok]).search("q")[0].url == "https://a.com/"
    assert FallbackSearch([Fake([]), ok]).search("q")[0].url == "https://a.com/"


def test_fallback_reports_every_failure_when_all_engines_fail():
    with pytest.raises(SearchError) as e:
        FallbackSearch([Fake(error=RuntimeError("quota exceeded")), Fake([])]).search("q")
    assert "quota exceeded" in str(e.value) and "no usable results" in str(e.value)
    with pytest.raises(SearchError, match="no search engine"):
        FallbackSearch([]).search("q")


def test_cache_reuses_results_expires_and_never_caches_failures():
    class Clock:
        t = 0.0
        def __call__(self): return self.t
    clk = Clock()
    inner = Fake([R("https://a.com/")])
    c = CachedSearch(inner, ttl=100, clock=clk)
    c.search("Solid State Batteries"); c.search("  solid state batteries ")
    assert inner.calls == 1                                    # case/space-insensitive
    clk.t = 101
    c.search("solid state batteries")
    assert inner.calls == 2                                    # expired
    bad = Fake(error=RuntimeError("down"))
    c2 = CachedSearch(bad, ttl=100, clock=clk)
    for _ in range(2):
        with pytest.raises(RuntimeError):
            c2.search("q")
    assert bad.calls == 2                                      # a failure must not poison the cache


def test_cache_is_bounded():
    c = CachedSearch(Fake([R("https://a.com/")]), maxsize=3)
    for i in range(20):
        c.search(f"query {i}")
    assert len(c._d) <= 3


def test_cached_results_are_copies():
    c = CachedSearch(Fake([R("https://a.com/")]))
    first = c.search("q")
    first.clear()
    assert len(c.search("q")) == 1


def test_builder_prefers_tavily_only_when_a_key_exists():
    names = lambda s: [type(e).__name__ for e in s.inner.engines]  # noqa: E731
    assert names(build_search("")) == ["MetaSearch"]
    assert names(build_search("tvly-test")) == ["TavilySearch", "MetaSearch"]


# ---------------------------------------------------------------- live (real network; skipped without a key)
@pytest.mark.skipif(not get_settings().tavily_api_key, reason="no Tavily key")
def test_live_tavily_returns_clean_cited_results():
    res = TavilySearch(get_settings().tavily_api_key).search("solid-state battery technology 2026", 5)
    assert 1 <= len(res) <= 5
    for r in res:
        assert safe_url(r.url) and not is_low_quality(r.url) and len(r.text) >= 40 and r.engine == "tavily"


def test_live_metasearch_fallback_finds_results():
    try:
        res = MetaSearch().search("python programming language", 4)
    except SearchError as e:  # public meta-search engines are flaky from some networks; report, don't hide
        pytest.skip(f"meta-search unavailable from this network: {e}")
    assert res and all(safe_url(r.url) for r in res) and res[0].engine.startswith("ddgs:")
