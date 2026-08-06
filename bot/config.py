"""Bot configuration. Reads Discord secrets via the shared resolver
(``pipeline_graph.discord_config``) and product knobs from ``pipeline_graph.config``.
Keeps the bot self-contained and standalone.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# MonkeForge root: bot/config.py -> bot/ -> MonkeForge/
MF_ROOT = Path(__file__).resolve().parents[1]
RUN_PY = MF_ROOT / "run.py"

# Target repo: same rules as run.py, non-interactive (bot cannot prompt).
# Precedence: PIPELINE_REPO env > single yaml repos: entry.
# N>1 without env → error (set PIPELINE_REPO).
if str(MF_ROOT) not in sys.path:
    sys.path.insert(0, str(MF_ROOT))
from pipeline_graph.repo_select import RepoSelectError, ensure_pipeline_repo  # noqa: E402

try:
    REPO = ensure_pipeline_repo(
        yaml_path=MF_ROOT / "monkeforge.yaml",
        mf_root=MF_ROOT,
        repo_flag=None,
        interactive=False,
    )
except RepoSelectError as exc:
    print(exc.cli_message(), file=sys.stderr)
    raise SystemExit(2) from None

from pipeline_graph import config as C  # noqa: E402
from pipeline_graph.discord_config import discord_secrets, resolve_discord_webhook  # noqa: E402

# Per-repo docs: MonkeForge/docs/<repo-name>/metrics/...
_repo_slug = REPO.name
DOCS = Path(os.environ.get("PIPELINE_DOCS_DIR") or (MF_ROOT / "docs" / _repo_slug))
EVENTS_LOG = DOCS / "metrics" / "events.jsonl"
STATE_FILE = DOCS / "metrics" / ".discord-bot-offset"


def _int(s: str) -> int | None:
    s = s.strip()
    return int(s) if s.isdigit() else None


# A GATEWAY bot token — NOT the webhook URL. Create a bot application at
# https://discord.com/developers, invite it to your server, put the token here.
_secrets = discord_secrets()
BOT_TOKEN = _secrets["bot_token"]
# Channel the bot posts escalations into (right-click channel → Copy ID).
CHANNEL_ID = _int(_secrets["channel_id"])
# Only these Discord user IDs may press buttons / run commands. Comma-separated.
ALLOWED_USER_IDS = {
    int(x) for x in _secrets["allowed_user_ids"].split(",")
    if x.strip().isdigit()
}

POLL_SECONDS = C.BOT_POLL_SECONDS
# run.py resume can take minutes (it drives the graph to the next pause); cap it.
RESUME_TIMEOUT = C.BOT_RESUME_TIMEOUT

# Webhook URL via the shared resolver (§3c/S1) — env DISCORD_WEBHOOK >
# repo/.discord-webhook file > "". The gateway bot itself posts via
# BOT_TOKEN/CHANNEL_ID; WEBHOOK is exposed so bot.py can fire webhook-based
# notifications through the same resolver as notify_daemon/run.py.
WEBHOOK = resolve_discord_webhook(repo=REPO)


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
