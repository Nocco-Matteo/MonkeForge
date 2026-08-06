"""The eyes runner — Playwright browser lifecycle, trace execution, facts/PNGs.

``run_eyes`` is called by ``quality_gates.ux_render`` (new-runner path) and by
``run.py::_run_eyes`` (diagnostic CLI leg). It:

1. Validates the selected ``ui_config`` (lazy, full structural) then the trace.
2. Spawns ``ui.start`` (cwd = ``ui.cwd`` or ``REPO``), polls ``ui.ready``.
3. Derives ``base_url`` per the brief table.
4. Runs ``auth_hooks.seed_script`` / ``require_e2e_db`` ONLY when declared
   (NOT a global ``_db_note`` prerequisite on the new runner).
5. Runs screens (1 execution in ``standard``, 2 in ``full``).
6. Captures global ``console_errors`` / ``failed_requests`` and per-screen
   ``page_loaded`` / ``screenshot_empty`` / ``has_overflow`` / ``layout_shifts``
   / ``is_stable`` (``full`` only) / ``interactive_count``.
7. Writes PNGs + ``monkeforge.eyes.facts/v1`` ``facts.json`` to
   ``{docs_dir}/reviews/screens/task-{tid}/``.
8. Process-tree cleanup on success / timeout / failure / escalation.

``type: electron`` → ``EyesError`` (clear message, no fallback). Returns a
facts dict + artifact paths + degradations + blocking flags per the gate-mode
policy table.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

from .. import config as C
from .discovery import DiscoveryCaps, DiscoveryResult, discover_screens
from .trace_validator import validate_trace


class EyesError(Exception):
    """A clear, non-fallback error from the eyes runner (e.g. Electron
    deferred, invalid config, infra crash)."""


# --- gate-mode policy -------------------------------------------------------

def _gate_policy(gate_mode: str) -> dict:
    """The per-mode policy flags (brief §3 "Per-mode policies")."""
    full = gate_mode == "full"
    return {
        "executions_per_screen": 2 if full else 1,
        "discovery_unverified_blocks": full,
        "trace_step_failure_blocks": full,
        "console_network_blocks": full,
        "page_loaded_blocks": full,
        "screenshot_empty_blocks": full,
        "interactive_count_blocks": False,  # informational even in full
        "layout_shifts_threshold": 0.1 if full else None,
        "is_stable_required": full,
    }


def _base_url(ui_config: dict) -> str:
    """Derive ``base_url`` per the brief table.

    - ``ui.url`` set → ``ui.url`` (origin rules: scheme + host + optional port)
    - ``ui.url`` absent, ``ui.start`` + ``ui.ready`` set → origin of ``ui.ready``
    - ``ui.ready`` not parseable http(s) on no-``url`` path → fail-loud
    """
    url = str(ui_config.get("url") or "").strip()
    if url:
        p = urlparse(url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
        return url
    ready = str(ui_config.get("ready") or "").strip()
    if ready:
        p = urlparse(ready)
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}"
        raise EyesError(
            f"ui.ready {ready!r} is not a parseable http(s) URL — cannot derive "
            f"base_url on the no-url path")
    raise EyesError("cannot derive base_url: neither ui.url nor ui.ready is set")


def _resolve_goto_url(raw_url: str, base_url: str) -> str:
    """Resolve a ``goto`` URL: absolute used as-is; path-relative joined to
    ``base_url``."""
    p = urlparse(raw_url)
    if p.scheme in ("http", "https"):
        return raw_url
    # path-relative → base_url + path
    from urllib.parse import urljoin
    return urljoin(base_url + "/", raw_url.lstrip("/"))


def _ready_timeout_s(ui_config: dict) -> int:
    raw = ui_config.get("ready_timeout_s")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    return min(C.UX_RENDER_TIMEOUT, 60)


def _viewport(ui_config: dict) -> tuple[int, int]:
    vp = ui_config.get("viewport")
    if isinstance(vp, dict) and "width" in vp and "height" in vp:
        try:
            return (int(vp["width"]), int(vp["height"]))
        except (TypeError, ValueError):
            pass
    return C.EYES_DEFAULT_VIEWPORT


def _spawn_start(ui_config: dict):
    """Spawn ``ui.start`` (cwd = ``ui.cwd`` or ``REPO``). Returns the Popen or
    None when ``ui.start`` is absent (``ui.url``-only path)."""
    start = str(ui_config.get("start") or "").strip()
    if not start:
        return None
    cwd = str(ui_config.get("cwd") or "").strip()
    cwd_path = (C.REPO / cwd).resolve() if cwd else C.REPO
    import shlex
    return subprocess.Popen(
        shlex.split(start), cwd=str(cwd_path),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _kill_tree(proc) -> None:
    """Kill the spawned process tree (process-group via setsid)."""
    if proc is None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.terminate()
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass


def _poll_ready(ui_config: dict, timeout_s: int) -> bool:
    """GET ``ui.ready`` every 500 ms; success on 2xx/3xx. Returns True on
    success, False on timeout."""
    import socket
    import urllib.request
    ready = str(ui_config.get("ready") or "").strip()
    if not ready:
        return True
    deadline = time.monotonic() + timeout_s
    interval = C.EYES_READY_POLL_INTERVAL_MS / 1000.0
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(ready, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if 200 <= resp.status < 400:
                    return True
        except (urllib.error.URLError, OSError, socket.timeout):
            pass
        time.sleep(interval)
    return False


def _run_auth_hooks(ui_config: dict) -> tuple[bool, str]:
    """Run ``auth_hooks.seed_script`` / ``require_e2e_db`` ONLY when declared.
    Returns (ok, error_msg)."""
    ah = ui_config.get("auth_hooks") or {}
    if not isinstance(ah, dict):
        return True, ""
    if ah.get("require_e2e_db"):
        if not C.db_reachable():
            return False, "auth_hooks.require_e2e_db: e2e DB is not reachable"
    seed = str(ah.get("seed_script") or "").strip()
    if seed:
        cwd = str(ui_config.get("cwd") or "").strip()
        cwd_path = (C.REPO / cwd).resolve() if cwd else C.REPO
        try:
            r = subprocess.run(
                ["bash", "-c", seed], cwd=str(cwd_path),
                capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                return False, f"auth_hooks.seed_script failed: {(r.stderr or r.stdout or '')[-300:]}"
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"auth_hooks.seed_script error: {exc}"
    return True, ""


def _facts_skeleton(tid: str, gate_mode: str, viewport: tuple[int, int]) -> dict:
    return {
        "schema": "monkeforge.eyes.facts/v1",
        "task_id": str(tid),
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "gate_mode": gate_mode,
        "console_errors": [],
        "failed_requests": [],
        "screens": {},
    }


def _playwright_discovery_fetcher():
    """Create a Playwright-based fetcher for same-origin discovery.

    Returns ``(fetcher, cleanup)`` or ``(None, None)`` when Playwright is
    unavailable or the browser cannot launch (discovery will be UNVERIFIED).
    The fetcher callable matches the ``discover_screens`` signature:
    ``(url) -> (status, html, final_url)``.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, None
    try:
        pw = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page()
    except Exception:  # noqa: BLE001 — browser launch failure is non-fatal
        return None, None

    def fetcher(url):
        resp = page.goto(url, wait_until="networkidle", timeout=30000)
        status = resp.status if resp else 0
        html = page.content()
        final_url = page.url
        return (status, html, final_url)

    def cleanup():
        try:
            browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            pw.stop()
        except Exception:  # noqa: BLE001
            pass

    return fetcher, cleanup


def _execute_trace(
    screens: list[dict],
    base_url: str,
    out_dir: Path,
    gate_mode: str,
    *,
    browser_runner=None,
    viewport: tuple[int, int] | None = None,
) -> tuple[dict, list[str]]:
    """Execute the trace (1 run in standard, 2 in full). Returns (facts_screens,
    png_paths).

    ``browser_runner``: an optional callable ``(screens, base_url, out_dir,
    gate_mode) -> (per_screen_facts, png_paths, console_errors, failed_requests)``
    injected by tests. When ``None``, the runner uses Playwright if available;
    otherwise it raises ``EyesError`` (no silent mock).
    """
    if browser_runner is not None:
        return browser_runner(screens, base_url, out_dir, gate_mode)
    # Real Playwright path — imported lazily so the module loads without
    # playwright installed (unit tests inject a browser_runner).
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise EyesError(
            "playwright is not installed — run `pip install playwright` "
            "and `playwright install chromium`") from exc
    policy = _gate_policy(gate_mode)
    execs = policy["executions_per_screen"]
    per_screen: dict[str, dict] = {}
    pngs: list[str] = []
    console_errors: list[str] = []
    failed_requests: list[str] = []
    vp = viewport or C.EYES_DEFAULT_VIEWPORT
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            for screen in screens:
                name = screen["name"]
                run_facts: list[dict] = []
                for ex in range(execs):
                    page = browser.new_page(viewport={"width": vp[0], "height": vp[1]})
                    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
                    page.on("requestfailed", lambda req: failed_requests.append(f"{req.method} {req.url} {req.failure}") if req.failure else None)
                    page_loaded = True
                    screenshot_empty = False
                    interactive_count = 0
                    has_overflow = False
                    trace_error: str | None = None
                    try:
                        for act in screen.get("actions", []):
                            a = act.get("action")
                            if a == "goto":
                                page.goto(_resolve_goto_url(act["url"], base_url), wait_until="networkidle")
                            elif a == "click":
                                page.click(act["selector"], timeout=act.get("timeout_ms", 30000))
                                interactive_count += 1
                            elif a == "fill":
                                page.fill(act["selector"], act["text"])
                                interactive_count += 1
                            elif a == "select":
                                page.select_option(act["selector"], act["value"])
                                interactive_count += 1
                            elif a == "press":
                                page.press(act["selector"], act["key"])
                                interactive_count += 1
                            elif a == "hover":
                                page.hover(act["selector"])
                            elif a == "scroll":
                                page.locator(act["selector"]).scroll_into_view_if_needed()
                                sx = int(act.get("x", 0))
                                sy = int(act.get("y", 0))
                                if sx or sy:
                                    page.mouse.wheel(sx, sy)
                                interactive_count += 1
                            elif a == "wait_for":
                                state = act.get("state", "visible")
                                page.wait_for_selector(act["selector"], state=state, timeout=act.get("timeout_ms", 30000))
                            elif a == "wait_ms":
                                page.wait_for_timeout(int(act["ms"]))
                            elif a == "screenshot":
                                full_page = bool(act.get("full_page", False))
                                shot_path = out_dir / f"{act['name']}.png"
                                page.screenshot(path=str(shot_path), full_page=full_page)
                                if shot_path.exists() and shot_path.stat().st_size > 0:
                                    pngs.append(str(shot_path))
                                else:
                                    screenshot_empty = True
                        # page_loaded: check via evaluate
                        page_loaded = bool(page.evaluate("() => document.readyState === 'complete'"))
                        has_overflow = bool(page.evaluate("() => document.documentElement.scrollWidth > document.documentElement.clientWidth"))
                    except Exception as exc:  # noqa: BLE001
                        page_loaded = False
                        trace_error = str(exc)
                    finally:
                        page.close()
                    run_facts.append({
                        "page_loaded": page_loaded,
                        "screenshot_empty": screenshot_empty,
                        "has_overflow": has_overflow,
                        "interactive_count": interactive_count,
                        "trace_error": trace_error,
                    })
                # Aggregate across executions.
                first = run_facts[0] if run_facts else {}
                screen_facts = {
                    "page_loaded": first.get("page_loaded", False),
                    "screenshot_empty": first.get("screenshot_empty", True),
                    "has_overflow": first.get("has_overflow", False),
                    "interactive_count": first.get("interactive_count", 0),
                    "trace_error": first.get("trace_error"),
                    "layout_shifts": 0.0,
                    "is_stable": True if execs == 1 else None,
                }
                if execs > 1 and len(run_facts) > 1:
                    # Structural stability: compare page_loaded, has_overflow,
                    # interactive_count, and screenshot_empty across runs.
                    # layout_shifts is the fraction of differing field checks
                    # (a proxy for CLS-like shift; 0.0 = identical runs).
                    fields = ("page_loaded", "has_overflow",
                              "interactive_count", "screenshot_empty")
                    total_checks = len(fields) * (len(run_facts) - 1)
                    diffs = sum(
                        1 for r in run_facts[1:]
                        for f in fields
                        if r.get(f) != first.get(f)
                    )
                    screen_facts["is_stable"] = diffs == 0
                    screen_facts["layout_shifts"] = round(
                        diffs / total_checks, 2) if total_checks > 0 else 0.0
                per_screen[name] = screen_facts
        finally:
            browser.close()
    return per_screen, pngs, console_errors, failed_requests


def run_eyes(
    tid: str,
    ui_config: dict,
    *,
    gate_mode: str,
    docs_dir: Path | None = None,
    repo: Path | None = None,
    browser_runner=None,
    discovery_fetcher=None,
) -> dict:
    """Run the eyes runner. Returns a result dict with facts, artifact paths,
    degradations, and blocking flags per the gate-mode policy.

    Parameters:
    - ``tid``: task id.
    - ``ui_config``: the selected ``ui:`` config (yaml or checkpointed state).
    - ``gate_mode``: ``off`` / ``standard`` / ``full``.
    - ``docs_dir``: where to write ``reviews/screens/task-{tid}/``. Defaults to
      ``C.DOCS`` (the CLI leg uses ``C.DOCS``; the graph node uses ``C.SCREENS``'
      parent which is ``C.REVIEWS``'s parent — i.e. ``C.DOCS``).
    - ``repo``: product repo root (defaults to ``C.REPO``).
    - ``browser_runner``: test-injected trace executor.
    - ``discovery_fetcher``: test-injected discovery fetcher
      ``(url) -> (status, html, final_url)``. When ``None`` and screens are
      absent, the runner wires a real Playwright-based fetcher (or UNVERIFIED
      if Playwright is unavailable).

    Returns a dict with: ``facts`` (the v1 facts dict), ``facts_json`` (str),
    ``screens_dir`` (Path), ``pngs`` (list[str]), ``degradations`` (list[str]),
    ``blocking`` (bool), ``escalation`` (str | None), ``unverified`` (bool).
    """
    if gate_mode == "off":
        return {
            "facts": _facts_skeleton(tid, gate_mode, _viewport(ui_config)),
            "facts_json": "{}",
            "screens_dir": None,
            "pngs": [],
            "degradations": ["eyes: gate mode off — skipped"],
            "blocking": False,
            "escalation": None,
            "unverified": False,
        }
    # Lazy full structural validation of the selected config.
    # Check for electron BEFORE validate_ui_config (which raises ValueError
    # for electron) so we raise EyesError with the clear deferred message.
    raw_type = str(ui_config.get("type") or "").strip()
    if raw_type == "electron":
        raise EyesError(
            "ui.type: electron is not supported in v1 (deferred). "
            "Use type: web (or auto, which resolves to web).")
    validated = C.validate_ui_config(ui_config)
    ui_type = validated.get("type", "web")
    # Resolve screens (yaml/state) or discover.
    screens = validated.get("screens") or []
    base_url = _base_url(validated)
    out_dir = (docs_dir or C.SCREENS).parent / f"task-{tid}" if docs_dir is None else (docs_dir / "reviews" / "screens" / f"task-{tid}")
    # The graph node path: C.SCREENS / f"task-{tid}" (SCREENS = DOCS/reviews/screens)
    if docs_dir is None:
        out_dir = C.SCREENS / f"task-{tid}"
    out_dir.mkdir(parents=True, exist_ok=True)
    # Clean prior artifacts.
    for p in list(out_dir.glob("*.png")):
        p.unlink()
    (out_dir / "facts.json").unlink(missing_ok=True)

    degradations: list[str] = []
    policy = _gate_policy(gate_mode)
    unverified = False
    escalation: str | None = None

    # Discovery when screens absent — wire a real Playwright fetcher so the
    # crawl actually performs network I/O (not a silent UNVERIFIED).
    if not screens:
        fetcher = discovery_fetcher
        cleanup = None
        if fetcher is None:
            fetcher, cleanup = _playwright_discovery_fetcher()
        try:
            disc = discover_screens(base_url, fetcher=fetcher)
        finally:
            if cleanup:
                cleanup()
        if disc.unverified or not disc.screens:
            unverified = True
            if policy["discovery_unverified_blocks"]:
                escalation = "eyes: discovery UNVERIFIED — blocked in full gate mode"
            else:
                degradations.append("shipped with UNVERIFIED UI discovery")
        else:
            screens = disc.screens
        for line in disc.caps_log:
            degradations.append(f"eyes {line}")

    # Validate the trace (closed allowlist) before browser launch.
    if screens:
        try:
            validate_trace(screens)
        except ValueError as exc:
            # Validation failure is fail-loud before browser launch.
            raise EyesError(f"trace validation failed: {exc}") from exc

    # Spawn ui.start + poll ready.
    proc = None
    try:
        if screens:
            proc = _spawn_start(validated)
            if proc is not None:
                timeout_s = _ready_timeout_s(validated)
                if not _poll_ready(validated, timeout_s):
                    escalation = (
                        f"eyes: ui.ready did not respond within {timeout_s}s "
                        f"— killed started process tree")
                    return {
                        "facts": _facts_skeleton(tid, gate_mode, _viewport(validated)),
                        "facts_json": "{}",
                        "screens_dir": out_dir,
                        "pngs": [],
                        "degradations": degradations,
                        "blocking": True,
                        "escalation": escalation,
                        "unverified": unverified,
                    }
            # auth_hooks (only when declared — NOT global _db_note).
            ok, err = _run_auth_hooks(validated)
            if not ok:
                escalation = f"eyes: {err}"
                return {
                    "facts": _facts_skeleton(tid, gate_mode, _viewport(validated)),
                    "facts_json": "{}",
                    "screens_dir": out_dir,
                    "pngs": [],
                    "degradations": degradations,
                    "blocking": True,
                    "escalation": escalation,
                    "unverified": unverified,
                }

        # Execute the trace.
        per_screen: dict = {}
        pngs: list[str] = []
        console_errors: list[str] = []
        failed_requests: list[str] = []
        if screens:
            try:
                per_screen, pngs, console_errors, failed_requests = _execute_trace(
                    screens, base_url, out_dir, gate_mode,
                    browser_runner=browser_runner,
                    viewport=_viewport(validated))
            except EyesError:
                raise
            except Exception as exc:  # noqa: BLE001
                escalation = f"eyes: infra crash during trace execution: {exc}"
                return {
                    "facts": _facts_skeleton(tid, gate_mode, _viewport(validated)),
                    "facts_json": "{}",
                    "screens_dir": out_dir,
                    "pngs": [],
                    "degradations": degradations,
                    "blocking": True,
                    "escalation": escalation,
                    "unverified": unverified,
                }

        # Build facts.
        facts = _facts_skeleton(tid, gate_mode, _viewport(validated))
        facts["console_errors"] = console_errors
        facts["failed_requests"] = failed_requests
        facts["screens"] = per_screen

        # Gate-mode blocking rules.
        blocking = False
        for name, sf in per_screen.items():
            if sf.get("trace_error"):
                if policy["trace_step_failure_blocks"]:
                    blocking = True
                    degradations.append(
                        f"eyes: screen {name} trace step failed — blocked in full")
                else:
                    degradations.append(
                        f"eyes: screen {name} trace step failed — degraded in standard")
            if policy["page_loaded_blocks"] and not sf.get("page_loaded", False):
                blocking = True
                degradations.append(f"eyes: screen {name} page_loaded != true — blocked in full")
            if policy["screenshot_empty_blocks"] and sf.get("screenshot_empty", False):
                blocking = True
                degradations.append(f"eyes: screen {name} screenshot empty — blocked in full")
            if policy["layout_shifts_threshold"] is not None:
                ls = sf.get("layout_shifts")
                if isinstance(ls, (int, float)) and ls > policy["layout_shifts_threshold"]:
                    blocking = True
                    degradations.append(f"eyes: screen {name} layout_shifts={ls} > 0.1 — blocked in full")
            if policy["is_stable_required"]:
                if sf.get("is_stable") is None:
                    blocking = True
                    degradations.append(f"eyes: screen {name} missing is_stable in full — blocked")
                elif not sf.get("is_stable"):
                    blocking = True
                    degradations.append(f"eyes: screen {name} is_stable != true — blocked in full")
        if console_errors:
            if policy["console_network_blocks"]:
                blocking = True
                degradations.append(f"eyes: {len(console_errors)} console error(s) — blocked in full")
            else:
                degradations.append(f"eyes: {len(console_errors)} console error(s) — degraded in standard")
        if failed_requests:
            if policy["console_network_blocks"]:
                blocking = True
                degradations.append(f"eyes: {len(failed_requests)} failed request(s) — blocked in full")
            else:
                degradations.append(f"eyes: {len(failed_requests)} failed request(s) — degraded in standard")
        if unverified and policy["discovery_unverified_blocks"]:
            blocking = True

        # Write facts.json.
        facts_path = out_dir / "facts.json"
        facts_path.write_text(json.dumps(facts, indent=2))

        if blocking and escalation is None:
            escalation = "eyes: gate-mode blocking rules triggered"

        return {
            "facts": facts,
            "facts_json": json.dumps(facts),
            "screens_dir": out_dir,
            "pngs": pngs,
            "degradations": degradations,
            "blocking": blocking,
            "escalation": escalation,
            "unverified": unverified,
        }
    finally:
        _kill_tree(proc)
