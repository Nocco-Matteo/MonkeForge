# Discord control bot (optional)

Manage pipeline **blocks** from a phone, anywhere — no VPN, no open ports. The
bot connects OUT to Discord, tails `docs/metrics/events.jsonl`, posts each
escalation as a card with one button per valid answer (and the screenshots for a
visual block), and runs `run.py resume …` when you tap. It also answers
`/status`, `/doctor`, `/resume`.

It **cannot start a run** — by design it only manages runs already under way.
MonkeForge stays standalone: the pipeline never imports this and runs fine without it.

## Setup (once)

1. Create a bot application at <https://discord.com/developers/applications> →
   Bot → copy the **token**. Enable no privileged intents (buttons + slash need
   none). Invite it to your server with the `applications.commands` scope and
   permission to send messages in one channel.
2. In Discord (Developer Mode on): right-click the target channel → **Copy ID**;
   right-click your user → **Copy ID**.
3. Add to `.env` (same file as the webhook):
   ```
   DISCORD_BOT_TOKEN=<the bot token>
   DISCORD_CHANNEL_ID=<channel id>
   DISCORD_ALLOWED_USER_IDS=<your user id>[,another]
   ```
4. Install the dep into the pipeline venv:
   ```
   ./venv/bin/pip install -r bot/requirements.txt
   ```

## Run

```bash
./venv/bin/python bot/bot.py
```

> **Standalone launches:** the bot reads `PIPELINE_DOCS_DIR` (or
> `PIPELINE_REPO`) to find the per-repo `docs/<repo>/metrics/` directory it
> tails. When `run.py` auto-starts the bot (`PIPELINE_BOT_AUTOSTART=1`) it
> passes `PIPELINE_DOCS_DIR` explicitly in the child env, so the two always
> agree. If you launch the bot by hand **outside** of `run.py`, export the
> same `PIPELINE_DOCS_DIR` (or `PIPELINE_REPO`) the pipeline is using, or
> the bot will watch the wrong metrics directory and its escalation cards
> will be silently absent while the webhook keeps posting (the safe
> direction — over-notify — but you lose the interactive buttons).
> ```bash
> export PIPELINE_DOCS_DIR=/path/to/MonkeForge/docs/<repo-name>
> ./venv/bin/python bot/bot.py
> ```

Under systemd so it survives (and keep the machine awake for runs):

```ini
# ~/.config/systemd/user/pipeline-bot.service
[Unit]
Description=Nexus pipeline Discord bot
[Service]
ExecStart=%h/Documenti/progetti/MonkeForge/venv/bin/python %h/Documenti/progetti/MonkeForge/bot/bot.py
Restart=always
[Install]
WantedBy=default.target
```
```
systemctl --user enable --now pipeline-bot
```

## What you get on the phone

- **Escalation/checkpoint** → an embed with the reason, where the run is, the
  valid answer menu, and (for an effort checkpoint) one button per effort level
  with the recommended level highlighted. Visual blocks also include screenshots.
  Tap a button → if a live `./run.py` session is waiting on that pause, the
  answer is delivered in-place (CLI unblocks); otherwise the bot runs
  `run.py resume --answer …`. Outcome is posted back in-channel.
- **`/status <id>`**, **`/doctor <id>`** — read state / what went wrong.
- **`/resume <id> [answer]`** — same delivery path as buttons. Old buttons still
  work after a bot restart (no more “interaction failed” on stale cards).

## Notes

- Synergy with an open CLI: pending answer file under `docs/.../metrics/`
  (`pending-answer-<id>.json`). Live session consumes it; no second graph driver.
- If no live session owns the pause, the bot spawns `run.py resume` as a
  subprocess (graph drives to the next pause and exits). The poller relays
  Discord cards; the pipeline itself is not a daemon.
- Config keys (all optional, env-overridable): `DISCORD_BOT_POLL_SECONDS` (5),
  `DISCORD_BOT_RESUME_TIMEOUT` (3600).
- The bot reuses the existing outbound webhook for plain notifications; it adds
  the interactive layer (buttons) on top. Lower `PIPELINE_NOTIFY_LEVEL` if the
  webhook duplicates feel noisy once the bot is running.
- Security: only `DISCORD_ALLOWED_USER_IDS` can press buttons or run `/resume`.
  Keep the bot in a private channel.
