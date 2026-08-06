"""Shared Discord webhook + secrets resolver (TASK-032 §3d).

Tests that ``resolve_discord_webhook`` and ``discord_secrets`` consolidate the
env > yaml > file resolution order for Discord configuration.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pipeline_graph import discord_config as dc


class TestResolveDiscordWebhook(unittest.TestCase):
    def test_env_wins_over_yaml(self):
        with patch.dict(os.environ, {"DISCORD_WEBHOOK": "https://env.example/hook"}, clear=False):
            with patch.object(dc, "_yaml_discord", return_value={"webhook": "https://yaml.example/hook"}):
                result = dc.resolve_discord_webhook()
        self.assertEqual(result, "https://env.example/hook")

    def test_yaml_wins_over_file(self):
        os.environ.pop("DISCORD_WEBHOOK", None)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".discord-webhook").write_text("https://file.example/hook")
            with patch.object(dc, "_yaml_discord", return_value={"webhook": "https://yaml.example/hook"}):
                result = dc.resolve_discord_webhook(repo=repo)
        self.assertEqual(result, "https://yaml.example/hook")

    def test_file_fallback(self):
        os.environ.pop("DISCORD_WEBHOOK", None)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            (repo / ".discord-webhook").write_text("https://file.example/hook\n")
            with patch.object(dc, "_yaml_discord", return_value={}):
                result = dc.resolve_discord_webhook(repo=repo)
        self.assertEqual(result, "https://file.example/hook")

    def test_empty_when_nothing_configured(self):
        os.environ.pop("DISCORD_WEBHOOK", None)
        with tempfile.TemporaryDirectory() as td:
            repo = Path(td)
            with patch.object(dc, "_yaml_discord", return_value={}):
                result = dc.resolve_discord_webhook(repo=repo)
        self.assertEqual(result, "")


class TestDiscordSecrets(unittest.TestCase):
    def test_env_wins_over_yaml(self):
        with patch.dict(os.environ, {
            "DISCORD_BOT_TOKEN": "env-token",
            "DISCORD_CHANNEL_ID": "111",
            "DISCORD_ALLOWED_USER_IDS": "222,333",
        }, clear=False):
            with patch.object(dc, "_yaml_discord", return_value={
                "bot_token": "yaml-token",
                "channel_id": "999",
                "allowed_user_ids": "888",
            }):
                result = dc.discord_secrets()
        self.assertEqual(result["bot_token"], "env-token")
        self.assertEqual(result["channel_id"], "111")
        self.assertEqual(result["allowed_user_ids"], "222,333")

    def test_yaml_fallback(self):
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        os.environ.pop("DISCORD_CHANNEL_ID", None)
        os.environ.pop("DISCORD_ALLOWED_USER_IDS", None)
        with patch.object(dc, "_yaml_discord", return_value={
            "bot_token": "yaml-token",
            "channel_id": "999",
            "allowed_user_ids": "888",
        }):
            result = dc.discord_secrets()
        self.assertEqual(result["bot_token"], "yaml-token")
        self.assertEqual(result["channel_id"], "999")
        self.assertEqual(result["allowed_user_ids"], "888")

    def test_empty_when_nothing_configured(self):
        os.environ.pop("DISCORD_BOT_TOKEN", None)
        os.environ.pop("DISCORD_CHANNEL_ID", None)
        os.environ.pop("DISCORD_ALLOWED_USER_IDS", None)
        with patch.object(dc, "_yaml_discord", return_value={}):
            result = dc.discord_secrets()
        self.assertEqual(result["bot_token"], "")
        self.assertEqual(result["channel_id"], "")
        self.assertEqual(result["allowed_user_ids"], "")


if __name__ == "__main__":
    unittest.main()
