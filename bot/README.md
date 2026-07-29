# Discord control bot (optional)

Manage pipeline **blocks** from a phone, anywhere — no VPN, no open ports. The
bot connects OUT to Discord, tails `docs/metrics/events.jsonl`, posts each
escalation as a card with one button per valid answer (and the screenshots for a
visual block), and runs `run.py resume …` when you tap. It also answers
`/status`, `/doctor`, `/resume`.

It **cannot start a run** — by design it only manages runs already under way.
lg stays standalone: the pipeline never imports this and runs fine without it.

## Setup (once)

1. Create a bot application at <https://discord.com/developers/applications> →
   Bot → copy the **token**. Enable no privileged intents (buttons + slash need
   none). Invite it to your server with the `applications.commands` scope and
   permission to send messages in one channel.
2. In Discord (Developer Mode on): right-click the target channel → **Copy ID**;
   right-click your user → **Copy ID**.
3. Add to `lg/.env` (same file as the webhook):
   ```
   DISCORD_BOT_TOKEN=<the bot token>
   DISCORD_CHANNEL_ID=<channel id>
   DISCORD_ALLOWED_USER_IDS=<your user id>[,another]
   ```
4. Install the dep into the pipeline venv:
   ```
   ./venv/bin/pip install -r lg/bot/requirements.txt
   ```

## Run

```bash
./venv/bin/python lg/bot/bot.py
```

Under systemd so it survives (and keep the machine awake for runs):

```ini
# ~/.config/systemd/user/pipeline-bot.service
[Unit]
Description=Nexus pipeline Discord bot
[Service]
ExecStart=%h/Documenti/progetti/dnn-vtt/nexus-vtt/venv/bin/python %h/Documenti/progetti/dnn-vtt/nexus-vtt/lg/bot/bot.py
Restart=always
[Install]
WantedBy=default.target
```
```
systemctl --user enable --now pipeline-bot
```

## What you get on the phone

- **Escalation** → an embed with the reason, where the run is, the answer menu,
  and (for a visual block) the screenshots. Tap a button → the bot resumes with
  that answer and replies with the outcome (the next escalation card, or done).
- **`/status <id>`**, **`/doctor <id>`** — read state / what went wrong.
- **`/resume <id> [answer]`** — the fallback if the buttons are gone (e.g. the
  bot restarted after the card was posted).

## Notes

- The bot spawns `run.py resume` as a subprocess; that process drives the graph
  to the next pause and exits, and the poller relays the result. The pipeline is
  not a daemon — the bot is the only long-lived piece.
- Config keys (all optional, env-overridable): `DISCORD_BOT_POLL_SECONDS` (5),
  `DISCORD_BOT_RESUME_TIMEOUT` (3600).
- The bot reuses the existing outbound webhook for plain notifications; it adds
  the interactive layer (buttons) on top. Lower `PIPELINE_NOTIFY_LEVEL` if the
  webhook duplicates feel noisy once the bot is running.
- Security: only `DISCORD_ALLOWED_USER_IDS` can press buttons or run `/resume`.
  Keep the bot in a private channel.
