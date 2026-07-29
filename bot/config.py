"""Bot configuration, from lg/.env (same file the pipeline uses) or the
environment. Keeps the bot self-contained and lg standalone.
"""
from __future__ import annotations

import os
from pathlib import Path

# Repo root: lg/bot/ -> lg/ -> repo.
REPO = Path(__file__).resolve().parents[2]
RUN_PY = REPO / "lg" / "run.py"
EVENTS_LOG = REPO / "docs" / "metrics" / "events.jsonl"
STATE_FILE = REPO / "docs" / "metrics" / ".discord-bot-offset"

# Load lg/.env (the run.py pattern), without overriding a real environment.
_env = REPO / "lg" / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())


def _int(name: str) -> int | None:
    v = os.environ.get(name, "").strip()
    return int(v) if v.isdigit() else None


# A GATEWAY bot token — NOT the webhook URL. Create a bot application at
# https://discord.com/developers, invite it to your server, put the token here.
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
# Channel the bot posts escalations into (right-click channel → Copy ID).
CHANNEL_ID = _int("DISCORD_CHANNEL_ID")
# Only these Discord user IDs may press buttons / run commands. Comma-separated.
ALLOWED_USER_IDS = {
    int(x) for x in os.environ.get("DISCORD_ALLOWED_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

POLL_SECONDS = int(os.environ.get("DISCORD_BOT_POLL_SECONDS", "5"))
# run.py resume can take minutes (it drives the graph to the next pause); cap it.
RESUME_TIMEOUT = int(os.environ.get("DISCORD_BOT_RESUME_TIMEOUT", "3600"))


def problems() -> list[str]:
    out = []
    if not BOT_TOKEN:
        out.append("DISCORD_BOT_TOKEN is not set (a gateway bot token, not the webhook)")
    if not CHANNEL_ID:
        out.append("DISCORD_CHANNEL_ID is not set")
    if not ALLOWED_USER_IDS:
        out.append("DISCORD_ALLOWED_USER_IDS is empty — nobody could act on escalations")
    if not RUN_PY.exists():
        out.append(f"run.py not found at {RUN_PY}")
    return out
