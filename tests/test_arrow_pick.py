"""Arrow-key pause picker + CSI cleanup for typed answers."""
from __future__ import annotations

import io
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def run_mod():
    import run as run_mod
    return run_mod


class TestCleanAnswer:
    def test_strips_csi_arrow_leftovers(self, run_mod):
        # Real failure mode: ↑ then typed "ok" → "\x1b[Aok" / "\x1b[a\x1b[bok"
        assert run_mod._clean_answer("\x1b[Aok") == "ok"
        assert run_mod._clean_answer("\x1b[a\x1b[bok") == "ok"
        assert run_mod._clean_answer("  ok  ") == "ok"

    def test_empty_after_strip(self, run_mod):
        assert run_mod._clean_answer("\x1b[A\x1b[B") == ""


class TestCanArrowPick:
    def test_false_on_stringio_stdin(self, run_mod, monkeypatch):
        monkeypatch.setattr("sys.stdin", io.StringIO("ok\n"))
        assert run_mod._can_arrow_pick() is False

    def test_false_on_dumb_term(self, run_mod, monkeypatch):
        monkeypatch.setenv("TERM", "dumb")
        # Even if isatty were true, dumb TERM must disable arrow mode.
        assert run_mod._can_arrow_pick() is False


class TestArrowPickKeys:
    def test_enter_selects_highlighted(self, run_mod, monkeypatch):
        """Simulate ↑/↓ menu: Down then Enter → second option."""
        opts = [
            {"key": "ok", "label": "go"},
            {"key": "replan", "label": "back"},
        ]
        data = {"hint": "ok", "stage": "plan approved?"}
        keys = iter(["DOWN", "ENTER"])

        monkeypatch.setattr(run_mod, "_can_arrow_pick", lambda: True)
        monkeypatch.setattr(run_mod, "_read_raw_key", lambda fd: next(keys))

        import select
        import termios
        import tty

        monkeypatch.setattr(termios, "tcgetattr", lambda fd: [])
        monkeypatch.setattr(termios, "tcsetattr", lambda *a, **k: None)
        monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
        monkeypatch.setattr(select, "select",
                            lambda *a, **k: ([0], [], []))
        monkeypatch.setattr("sys.stdin", MagicMock(
            fileno=MagicMock(return_value=0),
            isatty=MagicMock(return_value=True),
        ))
        monkeypatch.setattr("sys.stderr", MagicMock(
            isatty=MagicMock(return_value=True),
            write=MagicMock(),
            flush=MagicMock(),
        ))

        ans = run_mod._arrow_pick(opts, data, color=False)
        assert ans == "replan"

    def test_typing_is_ignored(self, run_mod, monkeypatch):
        """Arrow mode is arrows-only — printable keys must not select."""
        opts = [
            {"key": "ok", "label": "go"},
            {"key": "skip", "label": "stop"},
        ]
        data = {"hint": "ok"}
        # Type "skip" then Enter → still the highlighted hint row ("ok").
        keys = iter(["s", "k", "i", "p", "ENTER"])

        monkeypatch.setattr(run_mod, "_read_raw_key", lambda fd: next(keys))
        import select
        import termios
        import tty
        monkeypatch.setattr(termios, "tcgetattr", lambda fd: [])
        monkeypatch.setattr(termios, "tcsetattr", lambda *a, **k: None)
        monkeypatch.setattr(tty, "setcbreak", lambda fd: None)
        monkeypatch.setattr(select, "select",
                            lambda *a, **k: ([0], [], []))
        monkeypatch.setattr("sys.stdin", MagicMock(
            fileno=MagicMock(return_value=0),
            isatty=MagicMock(return_value=True),
        ))
        monkeypatch.setattr("sys.stderr", MagicMock(
            isatty=MagicMock(return_value=True),
            write=MagicMock(),
            flush=MagicMock(),
        ))

        ans = run_mod._arrow_pick(opts, data, color=False)
        assert ans == "ok"


class TestTtyPickFallsBack:
    def test_line_mode_cleans_csi(self, run_mod, monkeypatch):
        monkeypatch.setattr(run_mod, "_can_arrow_pick", lambda: False)
        monkeypatch.setattr(run_mod, "_read_answer_line",
                            lambda **kw: "\x1b[Aok")
        opts = [{"key": "ok", "label": "go"}, {"key": "skip", "label": "x"}]
        ans = run_mod._tty_pick(opts, {"hint": "ok"}, render=False)
        assert ans == "ok"
