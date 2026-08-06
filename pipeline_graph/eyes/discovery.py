"""Bounded same-origin link crawl for eyes discovery (TASK-012).

When ``ui.screens`` is absent after config resolution (yaml → state →
interrupt), the runner calls ``discover_screens(base_url)`` to crawl same-
origin ``http(s)`` links from ``base_url`` with hard caps. Emits the canonical
``ui.screens`` shape (``name`` + ``actions:[{action:goto,url},{action:screenshot,name}]``).

Caps (from ``config.EYES_DISCOVERY_*``):
- max pages/screens: 12
- max depth: 2
- wall-clock cap: 90 s
- max link expansions: 40

Skips: ``javascript:``, ``data:``, ``mailto:``, ``file:``, other origins.
Off-origin redirect targets are NOT enqueued. ``UNVERIFIED`` is returned (not
raised) when no validated trace is produced after the crawl.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from .. import config as C


_SKIP_SCHEMES = frozenset({"javascript", "data", "mailto", "file"})


def _origin(url: str) -> str:
    """``scheme://host[:port]`` — the same-origin comparison key."""
    p = urlparse(url)
    if not p.scheme or not p.netloc:
        return ""
    return f"{p.scheme}://{p.netloc}"


def _is_http(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)


def _same_origin(url: str, base_origin: str) -> bool:
    return _origin(url) == base_origin


def _canonical_name(url: str, used: set[str]) -> str:
    """A stable screen name from the URL path, deduplicated against ``used``."""
    p = urlparse(url)
    path = (p.path or "/").strip("/")
    if not path:
        base = "home"
    else:
        base = path.rstrip("/").split("/")[-1] or "home"
    # sanitize: keep alnum + dash, lowercase.
    base = "".join(c if c.isalnum() or c == "-" else "-" for c in base.lower()).strip("-") or "page"
    name = base
    n = 2
    while name in used:
        name = f"{base}-{n}"
        n += 1
    used.add(name)
    return name


@dataclass
class DiscoveryCaps:
    max_pages: int = C.EYES_DISCOVERY_MAX_PAGES
    max_depth: int = C.EYES_DISCOVERY_MAX_DEPTH
    timeout_s: int = C.EYES_DISCOVERY_TIMEOUT_S
    max_links: int = C.EYES_DISCOVERY_MAX_LINKS


@dataclass
class DiscoveryResult:
    screens: list[dict] = field(default_factory=list)
    unverified: bool = False
    caps_log: list[str] = field(default_factory=list)


def discover_screens(
    base_url: str,
    *,
    caps: DiscoveryCaps | None = None,
    fetcher=None,
) -> DiscoveryResult:
    """Same-origin ``http(s)`` link crawl from ``base_url``.

    ``fetcher``: an optional callable ``(url) -> (status, html, final_url)``
    injected by tests. When ``None``, discovery returns ``UNVERIFIED`` (no
    network I/O in unit tests / when Playwright is not wired). The runner
    passes a real fetcher (Playwright page).

    Returns a ``DiscoveryResult`` with:
    - ``screens``: canonical ``ui.screens`` list (may be empty).
    - ``unverified``: True when no validated trace was produced.
    - ``caps_log``: human-readable cap-hit lines for journal / doctor.
    """
    if caps is None:
        caps = DiscoveryCaps()
    result = DiscoveryResult()
    if not _is_http(base_url):
        result.unverified = True
        result.caps_log.append("discovery: base_url is not http(s)")
        return result
    base_origin = _origin(base_url)
    if not base_origin:
        result.unverified = True
        result.caps_log.append("discovery: base_url has no parseable origin")
        return result

    if fetcher is None:
        # No fetcher wired (unit-test / no-browser path): record UNVERIFIED.
        result.unverified = True
        result.caps_log.append("discovery: no fetcher — UNVERIFIED")
        return result

    used_names: set[str] = set()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(base_url, 0)]
    expansions = 0
    deadline = time.monotonic() + caps.timeout_s

    while queue:
        if len(result.screens) >= caps.max_pages:
            result.caps_log.append(
                f"discovery: stopped at max_pages={caps.max_pages}")
            break
        if expansions >= caps.max_links:
            result.caps_log.append(
                f"discovery: stopped at max_links={caps.max_links}")
            break
        if time.monotonic() > deadline:
            result.caps_log.append(
                f"discovery: stopped at timeout={caps.timeout_s}s")
            break
        url, depth = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        if not _same_origin(url, base_origin):
            continue
        try:
            status, html, final_url = fetcher(url)
        except Exception as exc:  # noqa: BLE001 — network errors are non-fatal
            result.caps_log.append(f"discovery: fetch error {url}: {exc}")
            continue
        expansions += 1
        # Record a screen for this page (even if it redirects off-origin —
        # the source page was fetched and is a valid screen; only the
        # off-origin target is not enqueued for further crawling).
        name = _canonical_name(final_url or url, used_names)
        result.screens.append({
            "name": name,
            "actions": [
                {"action": "goto", "url": final_url or url},
                {"action": "screenshot", "name": name},
            ],
        })
        # Off-origin redirect: do not enqueue the target for further crawling.
        if final_url and _is_http(final_url) and not _same_origin(final_url, base_origin):
            result.caps_log.append(f"discovery: off-origin redirect skipped from {url}")
            continue
        if depth >= caps.max_depth:
            continue
        # Extract same-origin links from the HTML (simple regex-free href scan).
        page_url = final_url or url
        for link in _extract_links(html or "", page_url, base_origin):
            if link not in visited:
                queue.append((link, depth + 1))

    if not result.screens:
        result.unverified = True
        result.caps_log.append("discovery: no pages crawled — UNVERIFIED")
    return result


def _extract_links(html: str, page_url: str, base_origin: str) -> list[str]:
    """Extract same-origin ``http(s)`` href links from ``html``.

    A simple, dependency-free href scanner (no full HTML parser). Skips
    ``javascript:``, ``data:``, ``mailto:``, ``file:``, and other-origin links.
    """
    links: list[str] = []
    seen: set[str] = set()
    idx = 0
    lower = html.lower()
    while True:
        pos = lower.find("href", idx)
        if pos == -1:
            break
        # find the = after href
        eq = lower.find("=", pos)
        if eq == -1:
            break
        # find the opening quote
        q_start = eq + 1
        while q_start < len(html) and html[q_start] in " \t":
            q_start += 1
        if q_start >= len(html) or html[q_start] not in "\"'":
            idx = pos + 4
            continue
        quote = html[q_start]
        end = html.find(quote, q_start + 1)
        if end == -1:
            break
        raw = html[q_start + 1:end]
        idx = end + 1
        raw = raw.strip()
        if not raw:
            continue
        scheme = urlparse(raw).scheme.lower()
        if scheme in _SKIP_SCHEMES:
            continue
        if scheme and scheme not in ("http", "https"):
            continue
        absolute = urljoin(page_url, raw)
        if not _is_http(absolute):
            continue
        if not _same_origin(absolute, base_origin):
            continue
        if absolute not in seen:
            seen.add(absolute)
            links.append(absolute)
    return links
