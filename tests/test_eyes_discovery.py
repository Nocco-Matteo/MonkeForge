"""Discovery tests for the eyes runner (TASK-012).

Bounded same-origin link crawl with hard caps. Uses an injected fetcher (no
network I/O). Tests caps, same-origin filtering, off-origin redirect skipping,
UNVERIFIED, and the canonical screen shape.
"""
import pytest

from pipeline_graph.eyes.discovery import (
    DiscoveryCaps,
    discover_screens,
    _extract_links,
    _origin,
    _is_http,
    _same_origin,
    _canonical_name,
)


_BASE = "http://127.0.0.1:3000"


def _html(links: list[str], base: str = _BASE) -> str:
    """Build a minimal HTML page with the given href links."""
    body = "".join(f'<a href="{l}">link</a>' for l in links)
    return f"<html><body>{body}</body></html>"


class TestOriginHelpers:
    def test_origin(self):
        assert _origin("http://127.0.0.1:3000/path") == "http://127.0.0.1:3000"
        assert _origin("https://example.com/x") == "https://example.com"
        assert _origin("not-a-url") == ""

    def test_is_http(self):
        assert _is_http("http://127.0.0.1:3000/")
        assert _is_http("https://example.com/")
        assert not _is_http("javascript:alert(1)")
        assert not _is_http("data:text/html,x")
        assert not _is_http("not-a-url")

    def test_same_origin(self):
        assert _same_origin("http://127.0.0.1:3000/a", "http://127.0.0.1:3000")
        assert not _same_origin("http://other.com/", "http://127.0.0.1:3000")

    def test_canonical_name_dedup(self):
        used: set[str] = set()
        assert _canonical_name("http://x/home", used) == "home"
        assert _canonical_name("http://x/home", used) == "home-2"
        assert _canonical_name("http://x/settings/", used) == "settings"


class TestDiscoverScreens:
    def test_no_fetcher_returns_unverified(self):
        r = discover_screens(_BASE)
        assert r.unverified is True
        assert r.screens == []

    def test_non_http_base_returns_unverified(self):
        r = discover_screens("not-a-url")
        assert r.unverified is True

    def test_single_page(self):
        def fetcher(url):
            return (200, _html([]), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        assert len(r.screens) == 1
        assert r.screens[0]["name"] == "home"
        assert r.screens[0]["actions"][0]["action"] == "goto"
        assert r.screens[0]["actions"][1]["action"] == "screenshot"

    def test_same_origin_links_crawled(self):
        pages = {
            _BASE: _html([_BASE + "/about", _BASE + "/settings"]),
            _BASE + "/about": _html([]),
            _BASE + "/settings": _html([]),
        }
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        names = {s["name"] for s in r.screens}
        assert "home" in names
        assert "about" in names
        assert "settings" in names

    def test_off_origin_links_skipped(self):
        pages = {
            _BASE: _html(["http://other.com/page", _BASE + "/local"]),
            _BASE + "/local": _html([]),
        }
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        names = {s["name"] for s in r.screens}
        assert "home" in names
        assert "local" in names
        # off-origin page not crawled
        assert "page" not in names

    def test_javascript_and_data_links_skipped(self):
        pages = {
            _BASE: _html(["javascript:alert(1)", "data:text/html,x", "mailto:a@b.c",
                          _BASE + "/real"]),
            _BASE + "/real": _html([]),
        }
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        names = {s["name"] for s in r.screens}
        assert "home" in names
        assert "real" in names

    def test_max_pages_cap(self):
        # Build a chain of pages that exceeds the cap.
        pages = {}
        for i in range(20):
            url = _BASE + f"/p{i}" if i > 0 else _BASE
            next_url = _BASE + f"/p{i+1}"
            pages[url] = _html([next_url])
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        caps = DiscoveryCaps(max_pages=5, max_depth=10, timeout_s=30, max_links=100)
        r = discover_screens(_BASE, caps=caps, fetcher=fetcher)
        assert len(r.screens) <= 5
        assert any("max_pages" in line for line in r.caps_log)

    def test_max_depth_cap(self):
        pages = {}
        for i in range(10):
            url = _BASE + f"/d{i}" if i > 0 else _BASE
            next_url = _BASE + f"/d{i+1}"
            pages[url] = _html([next_url])
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        caps = DiscoveryCaps(max_pages=100, max_depth=2, timeout_s=30, max_links=100)
        r = discover_screens(_BASE, caps=caps, fetcher=fetcher)
        # depth 0 (home) + depth 1 (d1) + depth 2 (d2) = 3 pages
        assert len(r.screens) == 3

    def test_off_origin_redirect_not_enqueued(self):
        def fetcher(url):
            if url == _BASE:
                return (302, "", "http://other.com/redirected")
            return (200, _html([]), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        # The home page is recorded (from the redirect source), but the
        # off-origin target is not enqueued.
        assert len(r.screens) == 1
        assert any("off-origin redirect" in line for line in r.caps_log)

    def test_fetch_error_non_fatal(self):
        call_count = [0]
        def fetcher(url):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("boom")
            return (200, _html([]), url)
        # Re-queue the base URL after the error — but discover_screens pops
        # from a queue, so the error is logged and the crawl continues with
        # any remaining queued URLs. With a single URL, the error leaves no
        # screens → UNVERIFIED.
        r = discover_screens(_BASE, fetcher=fetcher)
        assert r.unverified is True
        assert any("fetch error" in line for line in r.caps_log)

    def test_relative_links_resolved(self):
        pages = {
            _BASE: _html(["/about", "settings"]),
            _BASE + "/about": _html([]),
            _BASE + "/settings": _html([]),
        }
        def fetcher(url):
            return (200, pages.get(url, _html([])), url)
        r = discover_screens(_BASE, fetcher=fetcher)
        names = {s["name"] for s in r.screens}
        assert "about" in names
        assert "settings" in names


class TestExtractLinks:
    def test_basic_links(self):
        html = _html(["/a", "/b", "http://other.com/c"])
        links = _extract_links(html, _BASE, _origin(_BASE))
        assert _BASE + "/a" in links
        assert _BASE + "/b" in links
        # off-origin not included
        assert "http://other.com/c" not in links

    def test_skip_schemes(self):
        html = _html(["javascript:alert(1)", "data:text/html,x", "mailto:a@b.c"])
        links = _extract_links(html, _BASE, _origin(_BASE))
        assert links == []

    def test_dedup(self):
        html = _html(["/a", "/a", "/a"])
        links = _extract_links(html, _BASE, _origin(_BASE))
        assert links == [_BASE + "/a"]
