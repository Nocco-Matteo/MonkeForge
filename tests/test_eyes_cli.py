"""CLI tests for the ``./run.py eyes`` diagnostic leg (TASK-012).

Validates the eyes subparser, _run_eyes config resolution (yaml → state →
pause), _parse_eyes_answer, and the resume preflight eyes-pause-marker
dispatch. Uses argparse directly (no subprocess) for speed.
"""
import argparse
import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from pipeline_graph import config as C


def _build_eyes_parser():
    """A standalone replica of run.py's eyes subparser for unit testing."""
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    ey = sub.add_parser("eyes")
    ey.add_argument("task_id")
    ey.add_argument("--gate", dest="gate", default="standard",
                    choices=["off", "standard", "full"])
    ey.add_argument("--mode", dest="gate", default="standard",
                    choices=["off", "standard", "full"])
    ey.add_argument("--no-isolate", action="store_true")
    return p


class TestEyesSubparser:
    def test_eyes_subcommand_exists(self):
        p = _build_eyes_parser()
        ns = p.parse_args(["eyes", "T-1"])
        assert ns.cmd == "eyes"
        assert ns.task_id == "T-1"

    def test_eyes_default_gate_standard(self):
        p = _build_eyes_parser()
        ns = p.parse_args(["eyes", "T-1"])
        assert ns.gate == "standard"

    def test_eyes_gate_override(self):
        p = _build_eyes_parser()
        ns = p.parse_args(["eyes", "T-1", "--gate", "full"])
        assert ns.gate == "full"

    def test_eyes_mode_alias(self):
        p = _build_eyes_parser()
        ns = p.parse_args(["eyes", "T-1", "--mode", "off"])
        assert ns.gate == "off"

    def test_eyes_invalid_gate_rejected(self):
        p = _build_eyes_parser()
        with pytest.raises(SystemExit):
            p.parse_args(["eyes", "T-1", "--gate", "invalid"])


class TestParseEyesAnswer:
    def test_empty_returns_none(self):
        import run as R
        assert R._parse_eyes_answer("") is None
        assert R._parse_eyes_answer(None) is None

    def test_json_url(self):
        import run as R
        ans = '{"type": "web", "url": "http://127.0.0.1:3000/"}'
        cfg = R._parse_eyes_answer(ans)
        assert cfg is not None
        assert cfg["type"] == "web"
        assert cfg["url"] == "http://127.0.0.1:3000/"

    def test_kv_lines_start_ready(self):
        import run as R
        ans = "type: web\nstart: npm run dev\nready: http://127.0.0.1:3000/"
        cfg = R._parse_eyes_answer(ans)
        assert cfg is not None
        assert cfg["type"] == "web"
        assert cfg["start"] == "npm run dev"
        assert cfg["ready"] == "http://127.0.0.1:3000/"

    def test_missing_type_returns_none(self):
        import run as R
        ans = "url: http://x"
        assert R._parse_eyes_answer(ans) is None

    def test_start_only_returns_none(self):
        import run as R
        ans = "type: web\nstart: npm run dev"
        assert R._parse_eyes_answer(ans) is None

    def test_comments_skipped(self):
        import run as R
        ans = "# comment\ntype: web\nurl: http://x"
        cfg = R._parse_eyes_answer(ans)
        assert cfg is not None
        assert cfg["type"] == "web"


class TestRunEyesConfigResolution:
    def _args(self, tid="T-cli", gate="standard"):
        return argparse.Namespace(
            cmd="eyes", task_id=tid, gate=gate, no_isolate=False)

    def test_yaml_present_runs_eyes(self, tmp_path):
        """When yaml ui: is present, _run_eyes calls eyes.run_eyes directly."""
        import run as R
        args = self._args()
        mock_result = {
            "facts": {"schema": "monkeforge.eyes.facts/v1",
                      "gate_mode": "standard", "screens": {}},
            "facts_json": "{}", "pngs": [], "degradations": [],
            "blocking": False, "escalation": None, "unverified": False,
        }
        with patch.object(C, "UI_CONFIG",
                          {"type": "web", "url": "http://x"}), \
             patch("pipeline_graph.eyes.run_eyes", return_value=mock_result) as mock_run, \
             patch.object(C, "eyes_config_pause_path", return_value=tmp_path / "pause.json"), \
             patch("run.open_checkpointer", side_effect=Exception("no DB")):
            rc = R._run_eyes(args)
        assert rc == 0
        mock_run.assert_called_once()

    def test_no_config_writes_pause_marker(self, tmp_path):
        """When no yaml and no state, _run_eyes writes a pause marker and
        returns 0 (waiting for resume with --answer)."""
        import run as R
        args = self._args()
        pause_path = tmp_path / "pause.json"
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""), \
             patch.object(C, "eyes_config_pause_path", return_value=pause_path), \
             patch.object(C, "METRICS", tmp_path), \
             patch("run.open_checkpointer", side_effect=Exception("no DB")):
            rc = R._run_eyes(args)
        assert rc == 0
        assert pause_path.exists()
        data = json.loads(pause_path.read_text())
        assert data["stage"] == "eyes_config"

    def test_resume_with_answer_parses_and_runs(self, tmp_path):
        """When called with resume_answer, _run_eyes parses it and runs."""
        import run as R
        args = self._args()
        pause_path = tmp_path / "pause.json"
        pause_path.write_text(json.dumps({"stage": "eyes_config"}))
        mock_result = {
            "facts": {"schema": "monkeforge.eyes.facts/v1", "screens": {}},
            "facts_json": "{}", "pngs": [], "degradations": [],
            "blocking": False, "escalation": None, "unverified": False,
        }
        answer = "type: web\nurl: http://127.0.0.1:3000/"
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""), \
             patch.object(C, "eyes_config_pause_path", return_value=pause_path), \
             patch.object(C, "METRICS", tmp_path), \
             patch("pipeline_graph.eyes.run_eyes", return_value=mock_result) as mock_run, \
             patch("run.open_checkpointer", side_effect=Exception("no DB")):
            rc = R._run_eyes(args, resume_answer=answer)
        assert rc == 0
        mock_run.assert_called_once()
        # Pause marker cleared.
        assert not pause_path.exists()

    def test_resume_with_invalid_answer_returns_2(self, tmp_path):
        import run as R
        args = self._args()
        pause_path = tmp_path / "pause.json"
        pause_path.write_text(json.dumps({"stage": "eyes_config"}))
        with patch.object(C, "UI_CONFIG", {}), \
             patch.object(C, "UX_RENDER_CMD", ""), \
             patch.object(C, "eyes_config_pause_path", return_value=pause_path), \
             patch.object(C, "METRICS", tmp_path), \
             patch("run.open_checkpointer", side_effect=Exception("no DB")):
            rc = R._run_eyes(args, resume_answer="bad answer")
        assert rc == 2

    def test_eyes_error_returns_1(self, tmp_path):
        import run as R
        from pipeline_graph.eyes.runner import EyesError
        args = self._args()
        with patch.object(C, "UI_CONFIG",
                          {"type": "electron", "url": "http://x"}), \
             patch.object(C, "eyes_config_pause_path", return_value=tmp_path / "pause.json"), \
             patch("run.open_checkpointer", side_effect=Exception("no DB")):
            rc = R._run_eyes(args)
        assert rc == 1


class TestEyesConfigPausePath:
    def test_path_format(self):
        p = C.eyes_config_pause_path("T-1")
        assert "eyes-config-pause-T-1.json" in str(p)
