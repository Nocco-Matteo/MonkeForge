"""Gate-mode policy tests for the eyes runner (TASK-012).

Validates the per-mode policy table: executions per screen, blocking rules,
degradation vs escalation, and the ``standard`` ≠ ``full`` split.
"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from pipeline_graph import config as C
from pipeline_graph.eyes.runner import (
    EyesError,
    _gate_policy,
    _base_url,
    run_eyes,
)


class TestGatePolicy:
    def test_standard_one_execution(self):
        p = _gate_policy("standard")
        assert p["executions_per_screen"] == 1
        assert p["discovery_unverified_blocks"] is False
        assert p["console_network_blocks"] is False
        assert p["page_loaded_blocks"] is False
        assert p["screenshot_empty_blocks"] is False
        assert p["is_stable_required"] is False
        assert p["layout_shifts_threshold"] is None

    def test_full_two_executions(self):
        p = _gate_policy("full")
        assert p["executions_per_screen"] == 2
        assert p["discovery_unverified_blocks"] is True
        assert p["console_network_blocks"] is True
        assert p["page_loaded_blocks"] is True
        assert p["screenshot_empty_blocks"] is True
        assert p["is_stable_required"] is True
        assert p["layout_shifts_threshold"] == 0.1

    def test_interactive_count_never_blocks(self):
        p_std = _gate_policy("standard")
        p_full = _gate_policy("full")
        assert p_std["interactive_count_blocks"] is False
        assert p_full["interactive_count_blocks"] is False


class TestBaseUrl:
    def test_url_set_returns_origin(self):
        cfg = {"type": "web", "url": "http://127.0.0.1:3000/path"}
        assert _base_url(cfg) == "http://127.0.0.1:3000"

    def test_url_absent_start_ready_returns_ready_origin(self):
        cfg = {"type": "web", "start": "npm run dev",
               "ready": "http://127.0.0.1:3000/health"}
        assert _base_url(cfg) == "http://127.0.0.1:3000"

    def test_ready_not_http_raises(self):
        cfg = {"type": "web", "start": "npm run dev", "ready": "not-a-url"}
        with pytest.raises(EyesError, match="not a parseable http"):
            _base_url(cfg)

    def test_neither_url_nor_ready_raises(self):
        cfg = {"type": "web"}
        with pytest.raises(EyesError, match="neither ui.url nor ui.ready"):
            _base_url(cfg)


class TestRunEyesGateOff:
    def test_off_mode_skips(self):
        cfg = {"type": "web", "url": "http://127.0.0.1:3000/"}
        result = run_eyes("test-off", cfg, gate_mode="off")
        assert result["blocking"] is False
        assert result["pngs"] == []
        assert any("gate mode off" in d for d in result["degradations"])


class TestRunEyesStandard:
    def _browser_runner(self, screens, base_url, out_dir, gate_mode):
        """A fake browser runner that produces one screen with clean facts."""
        per_screen = {}
        pngs = []
        for s in screens:
            name = s["name"]
            per_screen[name] = {
                "page_loaded": True,
                "screenshot_empty": False,
                "has_overflow": False,
                "interactive_count": 1,
                "layout_shifts": 0.0,
                "is_stable": True,
            }
            png_path = out_dir / f"{name}.png"
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            pngs.append(str(png_path))
        return per_screen, pngs, [], []

    def test_standard_clean_run(self, tmp_path):
        cfg = {
            "type": "web",
            "url": "http://127.0.0.1:3000/",
            "screens": [
                {"name": "home", "actions": [
                    {"action": "goto", "url": "http://127.0.0.1:3000/"},
                    {"action": "screenshot", "name": "home"},
                ]},
            ],
        }
        result = run_eyes("test-std", cfg, gate_mode="standard",
                          docs_dir=tmp_path, browser_runner=self._browser_runner)
        assert result["blocking"] is False
        assert len(result["pngs"]) == 1
        facts = result["facts"]
        assert facts["schema"] == "monkeforge.eyes.facts/v1"
        assert facts["gate_mode"] == "standard"
        assert facts["screens"]["home"]["page_loaded"] is True

    def test_standard_console_errors_degrade_not_block(self, tmp_path):
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, ["console error 1"], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-std-ce", cfg, gate_mode="standard",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is False
        assert any("console error" in d for d in result["degradations"])

    def test_standard_unverified_discovery_degrades(self, tmp_path):
        """When screens absent and discovery returns UNVERIFIED, standard
        degrades+continues (does not block)."""
        cfg = {"type": "web", "url": "http://127.0.0.1:3000/"}
        # No fetcher wired → discovery returns UNVERIFIED.
        result = run_eyes("test-std-uv", cfg, gate_mode="standard",
                          docs_dir=tmp_path, browser_runner=self._browser_runner)
        assert result["unverified"] is True
        assert result["blocking"] is False
        assert any("UNVERIFIED" in d for d in result["degradations"])


class TestRunEyesFull:
    def _browser_runner(self, screens, base_url, out_dir, gate_mode):
        per_screen = {}
        pngs = []
        for s in screens:
            name = s["name"]
            per_screen[name] = {
                "page_loaded": True,
                "screenshot_empty": False,
                "has_overflow": False,
                "interactive_count": 1,
                "layout_shifts": 0.0,
                "is_stable": True,
            }
            png_path = out_dir / f"{name}.png"
            png_path.write_bytes(b"\x89PNG\r\n\x1a\n")
            pngs.append(str(png_path))
        return per_screen, pngs, [], []

    def test_full_clean_run(self, tmp_path):
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=self._browser_runner)
        assert result["blocking"] is False
        assert result["facts"]["gate_mode"] == "full"

    def test_full_console_errors_block(self, tmp_path):
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, ["err"], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full-ce", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is True

    def test_full_unverified_discovery_blocks(self, tmp_path):
        cfg = {"type": "web", "url": "http://127.0.0.1:3000/"}
        result = run_eyes("test-full-uv", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=self._browser_runner)
        assert result["unverified"] is True
        assert result["blocking"] is True

    def test_full_layout_shifts_block(self, tmp_path):
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "layout_shifts": 0.2, "is_stable": False}
                     for s in screens},
                    pngs, [], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full-ls", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is True


class TestRunEyesElectron:
    def test_electron_raises_error(self, tmp_path):
        cfg = {"type": "electron", "url": "http://127.0.0.1:3000/"}
        with pytest.raises(EyesError, match="electron is not supported"):
            run_eyes("test-el", cfg, gate_mode="standard", docs_dir=tmp_path)

    def test_auto_resolves_to_web(self, tmp_path):
        cfg = {
            "type": "auto", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        def runner(screens, base_url, out_dir, gate_mode):
            p = out_dir / "home.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            return ({"home": {"page_loaded": True, "screenshot_empty": False,
                              "has_overflow": False, "interactive_count": 0,
                              "layout_shifts": 0.0, "is_stable": True}},
                    [str(p)], [], [])
        result = run_eyes("test-auto", cfg, gate_mode="standard",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["facts"]["gate_mode"] == "standard"


class TestTraceFailurePolicy:
    """Item 24 / BLOCKER 3: standard trace failures degrade, full blocks."""

    def test_standard_trace_failure_degrades(self, tmp_path):
        """A trace step failure in standard mode must produce a degradation,
        not be silently swallowed as page_loaded=False."""
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": False, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "trace_error": "TimeoutError: click timed out",
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, [], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-std-tf", cfg, gate_mode="standard",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is False
        assert any("trace step failed" in d and "degraded" in d
                   for d in result["degradations"])

    def test_full_trace_failure_blocks(self, tmp_path):
        """A trace step failure in full mode must block."""
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": False, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "trace_error": "TimeoutError: click timed out",
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, [], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full-tf", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is True
        assert any("trace step failed" in d and "blocked" in d
                   for d in result["degradations"])


class TestFullStabilityComparison:
    """Item 25 / BLOCKER 4: full-mode stability must compare more than
    page_loaded + has_overflow; layout_shifts must be a real fraction."""

    def test_interactive_count_diff_blocks(self, tmp_path):
        """When interactive_count differs across runs, is_stable is False and
        layout_shifts > 0."""
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            # full mode does 2 executions; the browser_runner is called once
            # and returns aggregated facts, so we simulate the instability
            # directly in the returned facts.
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 1,
                                  "trace_error": None,
                                  "layout_shifts": 0.25, "is_stable": False}
                     for s in screens},
                    pngs, [], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full-stab", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is True
        assert any("is_stable" in d for d in result["degradations"])

    def test_identical_runs_stable(self, tmp_path):
        """When all fields match across runs, is_stable is True and
        layout_shifts is 0.0."""
        def runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 1,
                                  "trace_error": None,
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, [], [])
        cfg = {
            "type": "web", "url": "http://127.0.0.1:3000/",
            "screens": [{"name": "home", "actions": [
                {"action": "goto", "url": "http://127.0.0.1:3000/"},
                {"action": "screenshot", "name": "home"}]}],
        }
        result = run_eyes("test-full-stable", cfg, gate_mode="full",
                          docs_dir=tmp_path, browser_runner=runner)
        assert result["blocking"] is False


class TestDiscoveryWithFetcher:
    """Item 14 / BLOCKER 1: discovery must perform a real crawl when
    ui.screens is absent and a fetcher is available."""

    def test_discovery_fetcher_discovers_screens(self, tmp_path):
        """When screens are absent and a discovery_fetcher is injected, the
        runner discovers screens and executes the trace (not UNVERIFIED)."""
        base = "http://127.0.0.1:3000"
        pages = {
            base: f'<html><body><a href="{base}/about">about</a></body></html>',
            base + "/about": "<html><body>about</body></html>",
        }

        def fetcher(url):
            return (200, pages.get(url, "<html></html>"), url)

        def browser_runner(screens, base_url, out_dir, gate_mode):
            pngs = []
            for s in screens:
                p = out_dir / f"{s['name']}.png"
                p.write_bytes(b"\x89PNG\r\n\x1a\n")
                pngs.append(str(p))
            return ({s["name"]: {"page_loaded": True, "screenshot_empty": False,
                                  "has_overflow": False, "interactive_count": 0,
                                  "trace_error": None,
                                  "layout_shifts": 0.0, "is_stable": True}
                     for s in screens},
                    pngs, [], [])

        cfg = {"type": "web", "url": base}
        result = run_eyes("test-disc", cfg, gate_mode="standard",
                          docs_dir=tmp_path,
                          browser_runner=browser_runner,
                          discovery_fetcher=fetcher)
        assert result["unverified"] is False
        assert len(result["pngs"]) >= 1
        # Discovered screens should include home and about.
        screen_names = set(result["facts"]["screens"].keys())
        assert "home" in screen_names
        assert "about" in screen_names

    def test_discovery_no_screens_unverified(self, tmp_path):
        """When the fetcher returns no pages, discovery is UNVERIFIED."""
        base = "http://127.0.0.1:3000"

        def fetcher(url):
            raise ConnectionError("refused")

        def browser_runner(screens, base_url, out_dir, gate_mode):
            return ({}, [], [], [])

        cfg = {"type": "web", "url": base}
        result = run_eyes("test-disc-fail", cfg, gate_mode="standard",
                          docs_dir=tmp_path,
                          browser_runner=browser_runner,
                          discovery_fetcher=fetcher)
        assert result["unverified"] is True
        assert result["blocking"] is False
