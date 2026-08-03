#!/usr/bin/env python3
"""Discord control plane for the pipeline — manage escalations from a phone.

Run:  python bot/bot.py     (needs discord.py; see requirements.txt / README)

It connects OUT to Discord (no open ports, no VPN), tails events.jsonl, and:
  - posts each escalation as a card with one BUTTON per valid answer (from the
    run_paused event's `answers` menu), plus the screenshots for a visual block;
  - on a button press (allowed users only) runs `run.py resume <id> --answer X`;
  - answers /status, /doctor, /resume.

It cannot start a run — only manage blocks of runs already under way.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# Also add the MonkeForge root so `pipeline_graph` is importable for
# button_specs (the shared option→button-spec translator).
_MF_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _MF_ROOT not in sys.path:
    sys.path.insert(0, _MF_ROOT)
import config as C  # noqa: E402
from pipeline_graph.nodes.common import button_specs  # noqa: E402

try:
    import discord
    from discord import app_commands
except ImportError:
    sys.exit("discord.py not installed — pip install -r bot/requirements.txt")


intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _allowed(user_id: int) -> bool:
    return user_id in C.ALLOWED_USER_IDS


async def _cli(*args: str) -> str:
    """Run `run.py <args>` and return its (trimmed) output. resume can take minutes."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(C.RUN_PY), *args, cwd=str(C.MF_ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=C.RESUME_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        return "(command timed out)"
    return (out or b"").decode("utf-8", "replace")


class AnswerButton(discord.ui.Button):
    def __init__(self, task_id: str, spec: dict, recommended: str = ""):
        answer = spec.get("answer") or spec.get("key", "ok")
        label = (spec.get("key") or "ok")[:80]
        super().__init__(label=label,
                         style=(discord.ButtonStyle.success
                                if answer == recommended else discord.ButtonStyle.primary),
                         custom_id=f"resume:{task_id}:{answer}")
        self.task_id, self.answer, self.meaning = task_id, answer, spec.get("label", "")

    async def callback(self, interaction: discord.Interaction):
        if not _allowed(interaction.user.id):
            await interaction.response.send_message("Not authorised.", ephemeral=True)
            return
        await interaction.response.edit_message(
            content=f"▶️ task {self.task_id}: resuming with `{self.answer}` "
                    f"({self.meaning})…", view=None)

        async def _run():
            out = await _cli("resume", self.task_id, "--answer", self.answer)
            tail = out.strip()[-1500:]
            await interaction.channel.send(
                f"task {self.task_id} — resumed with `{self.answer}`.\n```\n{tail}\n```")

        interaction.client.loop.create_task(_run())


class AnswerView(discord.ui.View):
    def __init__(self, task_id: str, options: list, recommended: str = ""):
        super().__init__(timeout=None)
        specs = button_specs(options)
        for spec in specs[:5]:   # Discord: 5 buttons/row
            self.add_item(AnswerButton(task_id, spec, recommended))


async def _post_escalation(channel, rec: dict):
    tid = rec.get("task", "?")
    stage = str(rec.get("step", ""))
    reason = rec.get("msg") or "waiting for a human"
    options = rec.get("options") or []
    # Backward compat: older events.jsonl records carry a legacy ``answers``
    # dict instead of the structured ``options`` list. Synthesize a minimal
    # options list so button_specs always has something to render.
    if not options and rec.get("answers"):
        options = [{"key": k, "label": str(v), "free_text": False}
                   for k, v in rec["answers"].items()]
    if not options:
        options = [{"key": "ok", "label": "continue", "free_text": False},
                   {"key": "skip", "label": "force-close", "free_text": False}]
    blockers = rec.get("blockers") or ""
    if stage == "effort level":
        title = f"⏱ TASK-{tid}: choose effort"
    else:
        title = f"⛔ TASK-{tid} needs you"
    embed = discord.Embed(title=title, description=reason, color=0xE74C3C)
    if rec.get("hint"):
        embed.add_field(name="recommended", value=str(rec["hint"]), inline=False)
    if rec.get("context"):
        embed.add_field(name="where", value=str(rec["context"])[:1024], inline=False)
    if blockers:
        embed.add_field(name="blockers", value=blockers[:1024], inline=False)
    embed.add_field(name="answers",
                    value="\n".join(f"**{o.get('key', '?')}** — {o.get('label', '')}"
                                    for o in options)[:1024],
                    inline=False)
    files = []
    screens = rec.get("screens")
    if screens:
        d = C.REPO / screens
        if d.is_dir():
            files = [discord.File(p) for p in sorted(d.glob("*.png"))[:4]]
    await channel.send(embed=embed, view=AnswerView(tid, options, str(rec.get("hint", ""))), files=files)


async def _post_end(channel, rec: dict):
    tid = rec.get("task", "?")
    degr = rec.get("degradations") or []
    embed = discord.Embed(title=f"✅ TASK-{tid} complete",
                          description=rec.get("msg", ""), color=0x2ECC71)
    if degr:
        embed.add_field(name=f"degradations shipped ({len(degr)})",
                        value="\n".join(f"• {d}" for d in degr)[:1024], inline=False)
    await channel.send(embed=embed)


async def _poller():
    """Tail events.jsonl from EOF; relay escalations and completions."""
    await client.wait_until_ready()
    channel = client.get_channel(C.CHANNEL_ID)
    if channel is None and client.guilds:
        # In a server but not cached: hit the API to tell "wrong id" from
        # "no View Channel permission".
        try:
            channel = await client.fetch_channel(C.CHANNEL_ID)
        except discord.NotFound:
            print(f"channel {C.CHANNEL_ID}: NOT FOUND — wrong id (copy a TEXT channel id).")
        except discord.Forbidden:
            print(f"channel {C.CHANNEL_ID}: FORBIDDEN — add the bot's role to this "
                  "channel's permissions (View Channel + Send Messages).")
        except Exception as exc:
            print(f"channel {C.CHANNEL_ID}: {type(exc).__name__}: {exc}")
    if channel is None:
        if not client.guilds:
            print("Bot is in NO server — the invite never completed. Re-open the "
                  "OAuth2 URL with BOTH scopes `bot` and `applications.commands` "
                  "and authorise it onto your server.")
        else:
            print(f"\nBot is in {len(client.guilds)} server(s). Text channels it can see:")
            for g in client.guilds:
                for ch in getattr(g, "text_channels", []):
                    print(f"  {g.name}: #{ch.name}  id={ch.id}")
            print("Put the right id in DISCORD_CHANNEL_ID and restart.")
        return
    # Start at end of file (or a saved offset) so we don't replay history.
    try:
        offset = int(C.STATE_FILE.read_text())
    except (OSError, ValueError):
        offset = C.EVENTS_LOG.stat().st_size if C.EVENTS_LOG.exists() else 0
    await channel.send("🤖 pipeline bot online — I'll post escalations here.")
    while not client.is_closed():
        try:
            if C.EVENTS_LOG.exists():
                with C.EVENTS_LOG.open() as f:
                    f.seek(offset)
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except ValueError:
                            continue
                        if rec.get("kind") == "run_paused":
                            await _post_escalation(channel, rec)
                        elif rec.get("kind") == "run_end":
                            await _post_end(channel, rec)
                    offset = f.tell()
                C.STATE_FILE.write_text(str(offset))
        except Exception as exc:            # a poll error must never kill the bot
            print("poller error:", exc)
        await asyncio.sleep(C.POLL_SECONDS)


@tree.command(description="Pipeline status for a task")
async def status(interaction: discord.Interaction, task_id: str):
    await interaction.response.defer()
    out = await _cli("status", task_id)
    await interaction.followup.send(f"```\n{out.strip()[-1800:]}\n```")


@tree.command(description="What went wrong for a task (doctor)")
async def doctor(interaction: discord.Interaction, task_id: str):
    await interaction.response.defer()
    out = await _cli("doctor", task_id)
    await interaction.followup.send(f"```\n{out.strip()[-1800:]}\n```")


@tree.command(description="Resume a paused task with an answer")
async def resume(interaction: discord.Interaction, task_id: str, answer: str = "ok"):
    if not _allowed(interaction.user.id):
        await interaction.response.send_message("Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()

    async def _run():
        out = await _cli("resume", task_id, "--answer", answer)
        tail = out.strip()[-1500:]
        await interaction.channel.send(
            f"task {task_id} — resumed with `{answer}`.\n```\n{tail}\n```")

    interaction.client.loop.create_task(_run())
    await interaction.followup.send(f"▶️ task {task_id}: resuming with `{answer}`… (output will follow)")


@tree.command(description="Stop the running pipeline for a task")
async def stop(interaction: discord.Interaction, task_id: str):
    if not _allowed(interaction.user.id):
        await interaction.response.send_message("Not authorised.", ephemeral=True)
        return
    await interaction.response.defer()
    import json as _json
    import os as _os
    import signal as _signal
    cur = C.REPO / "docs" / "metrics" / "current.json"
    if not cur.exists():
        await interaction.followup.send(f"No running pipeline found for task {task_id}.")
        return
    try:
        info = _json.loads(cur.read_text())
    except (OSError, ValueError):
        await interaction.followup.send("Could not read current.json — pipeline state unknown.")
        return
    pid = info.get("pid")
    if not pid:
        await interaction.followup.send("No PID in current.json — nothing to stop.")
        return
    try:
        _os.kill(int(pid), _signal.SIGTERM)
    except ProcessLookupError:
        await interaction.followup.send(f"Process {pid} not found — pipeline may have already exited.")
        return
    except PermissionError:
        await interaction.followup.send(f"Permission denied killing PID {pid}.")
        return
    await interaction.followup.send(f"⛔ Sent SIGTERM to pipeline process {pid} for task {task_id}.")


@tree.command(description="Show debate blockers for a task")
async def debate(interaction: discord.Interaction, task_id: str):
    await interaction.response.defer()
    debate_path = C.REPO / "docs" / "debates" / f"DEBATE-{task_id}.md"
    if not debate_path.exists():
        await interaction.followup.send(f"No debate file for task {task_id}.")
        return
    lines = debate_path.read_text().splitlines()
    # Collect the LAST section for each reviewer type (Reviewer, UX).
    # Earlier rounds' blockers were likely resolved — showing them is confusing.
    # Item 37: match the three provenance tag forms — bare [BLOCKER],
    # [BLOCKER:PLAN], [BLOCKER:REQUIREMENTS] — so a provenance-tagged blocker
    # surfaces in the Discord card the same as a bare one.
    blocker_re = re.compile(r"\[BLOCKER(?::(?:PLAN|REQUIREMENTS))?\]", re.IGNORECASE)
    last_sections: dict[str, list[str]] = {}
    cur_type = None
    cur_lines: list[str] = []
    for line in lines:
        if line.startswith("## Round ") and ("Reviewer" in line or "UX" in line):
            if cur_type and cur_lines:
                last_sections[cur_type] = cur_lines
            cur_type = "UX" if "UX" in line else "Reviewer"
            cur_lines = [f"\n**{line.strip()}**"]
            continue
        if cur_type and line.startswith("## "):
            if cur_lines:
                last_sections[cur_type] = cur_lines
            cur_type = None
            cur_lines = []
            continue
        if cur_type and (blocker_re.search(line) or "VERDICT:" in line or line.startswith("RESOLVED") or line.startswith("STILL OPEN")):
            cur_lines.append(line.strip())
    if cur_type and cur_lines:
        last_sections[cur_type] = cur_lines
    if not last_sections:
        await interaction.followup.send(f"No debate rounds found for task {task_id}.")
        return
    blockers: list[str] = []
    for rtype in ("Reviewer", "UX"):
        if rtype in last_sections:
            blockers.extend(last_sections[rtype])
    if not blockers:
        await interaction.followup.send(f"No blockers found in debate for task {task_id}.")
        return
    full = "\n".join(blockers)
    pages = [full[i:i+1800] for i in range(0, len(full), 1800)]
    await interaction.followup.send(f"**Debate blockers (latest round) — TASK-{task_id}** ({len(pages)} page(s))\n{pages[0]}")
    for page in pages[1:]:
        await interaction.channel.send(page)


@client.event
async def on_ready():
    import os as _os
    _pidfile = C.REPO / "docs" / "metrics" / ".bot.pid"
    _pidfile.parent.mkdir(parents=True, exist_ok=True)
    _pidfile.write_text(str(_os.getpid()))
    await tree.sync()
    client.loop.create_task(_poller())
    print(f"bot ready as {client.user} (pid {_os.getpid()})")


def main() -> int:
    probs = C.problems()
    if probs:
        print("bot config incomplete:")
        for p in probs:
            print(" -", p)
        return 2
    client.run(C.BOT_TOKEN)
    return 0


if __name__ == "__main__":
    sys.exit(main())
