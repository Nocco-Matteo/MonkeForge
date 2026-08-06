"""Shared Discord webhook + secrets resolver (TASK-032 §3d).

Single source for the webhook URL and the bot gateway secrets so ``run.py``,
``notify_daemon.py``, and ``bot/config.py`` stop duplicating the lookup. The
webhook is a SECRET — it is never allowlisted into the product-knob env path.
Resolution order for the webhook:

  1. ``DISCORD_WEBHOOK`` in the real process env (exported by the operator) —
     secrets stay env-owned (no ``.env`` file is read).
  2. ``discord.webhook`` in ``monkeforge.yaml`` (kept for legacy installs that
     checked it into a gitignored yaml).
  3. ``.discord-webhook`` file in the repo root (the original opt-in path).

The bot gateway secrets (``bot_token``, ``channel_id``, ``allowed_user_ids``)
follow the same env-first-then-yaml order. Non-secret discord product knobs
(``bot_name``, ``bot_avatar``, ``bot_poll_seconds``, ``resume_timeout``,
``bot_autostart``) live on ``pipeline_graph.config`` (§3d).
"""
from __future__ import annotations

import os
from pathlib import Path


# Config-independence (checklist item 7): the resolver MUST NOT import
# ``pipeline_graph.config`` — that module requires PIPELINE_REPO and a fully
# populated agents: block, so importing it here would make webhook/secrets
# lookup transitively require the whole product config. Instead we read the
# ``discord:`` mapping straight from the same yaml file config.py would load
# (PIPELINE_WT_YAML override, else MF_ROOT/monkeforge.yaml), with no config
# import side effects.
_MF_ROOT = Path(__file__).resolve().parents[1]


def _yaml_discord() -> dict:
    """The ``discord:`` mapping from the orchestrator yaml, or ``{}``.

    Reads the yaml directly (no ``pipeline_graph.config`` import) so importing
    this resolver never triggers repo/agent config validation.
    """
    override = (os.environ.get("PIPELINE_WT_YAML") or "").strip()
    yaml_file = Path(override).expanduser() if override else (_MF_ROOT / "monkeforge.yaml")
    if not yaml_file.is_file():
        return {}
    try:
        import yaml as _yaml
        data = _yaml.safe_load(yaml_file.read_text()) or {}
    except (OSError, Exception):
        return {}
    if not isinstance(data, dict):
        return {}
    node = data.get("discord")
    return node if isinstance(node, dict) else {}


def resolve_discord_webhook(*, repo: Path | None = None) -> str:
    """The Discord webhook URL, or ``""`` when none is configured.

    ``repo`` defaults to ``C.REPO`` (the ``.discord-webhook`` file lives in the
    target repo root, not MF_ROOT).
    """
    wh = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if wh:
        return wh
    wh = str(_yaml_discord().get("webhook", "") or "").strip()
    if wh:
        return wh
    if repo is None:
        # Config-independence: read PIPELINE_REPO directly instead of
        # importing pipeline_graph.config (which would trigger full config
        # validation). The .discord-webhook file lives in the target repo
        # root, set by run.py before any resolver call.
        _repo_env = (os.environ.get("PIPELINE_REPO") or "").strip()
        repo = Path(_repo_env).expanduser() if _repo_env else _MF_ROOT
    wh_file = repo / ".discord-webhook"
    if wh_file.exists():
        return wh_file.read_text().strip()
    return ""


def discord_secrets(*, repo: Path | None = None) -> dict[str, str]:
    """The bot gateway secrets: ``bot_token``, ``channel_id``, ``allowed_user_ids``.

    Each is env-first (``DISCORD_BOT_TOKEN`` / ``DISCORD_CHANNEL_ID`` /
    ``DISCORD_ALLOWED_USER_IDS``), then yaml ``discord.bot_token`` /
    ``discord.channel_id`` / ``discord.allowed_user_ids``. Missing values are
    ``""``. ``channel_id`` / ``allowed_user_ids`` are returned as strings
    (callers parse them) — yaml may quote them to preserve leading-digit shape.
    """
    y = _yaml_discord()
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        token = str(y.get("bot_token", "") or "").strip()
    channel = os.environ.get("DISCORD_CHANNEL_ID", "").strip()
    if not channel:
        channel = str(y.get("channel_id", "") or "").strip()
    allowed = os.environ.get("DISCORD_ALLOWED_USER_IDS", "").strip()
    if not allowed:
        allowed = str(y.get("allowed_user_ids", "") or "").strip()
    return {
        "bot_token": token,
        "channel_id": channel,
        "allowed_user_ids": allowed,
    }
