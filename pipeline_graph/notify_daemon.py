"""Persistent notification daemon: priority queue + sliding-window rate limiter.

Replaces the per-call notify.sh subprocess with a long-lived process that:
  - listens on a unix socket for JSON notifications
  - drains a priority queue (urgent > high > default > low)
  - enforces a sliding-window rate limit (30 msgs / 60s by default)
  - handles 429 Retry-After from Discord
  - persists the queue on shutdown and recovers on startup
  - falls back to a spool dir if the daemon is down (events.py handles this)

Usage:
  ./run.py notify-daemon           # foreground
  ./run.py notify-daemon --status  # heartbeat + queue length
  ./run.py notify-daemon --stop    # SIGTERM for clean shutdown
"""
from __future__ import annotations

import heapq
import json
import os
import signal
import socket
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from . import config as C

# Load .env from MonkeForge root so the daemon works standalone.
_MF_ROOT = Path(__file__).resolve().parents[1]
_env = _MF_ROOT / ".env"
if _env.exists():
    for _line in _env.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

# --- Config ---------------------------------------------------------------

RATE = int(os.environ.get("PIPELINE_NOTIFY_RATE", "30"))
WINDOW = int(os.environ.get("PIPELINE_NOTIFY_WINDOW", "60"))
SOCKET_PATH = Path(os.environ.get(
    "PIPELINE_NOTIFY_SOCKET") or (C.METRICS / "notify.sock"))
QUEUE_FILE = C.METRICS / "notify.queue.json"
SPOOL_DIR = C.METRICS / "notify.spool"
HEARTBEAT_FILE = C.METRICS / "notify.heartbeat"
LOG_FILE = C.METRICS / "notify.log"

PRIO_ORDER = {"urgent": 0, "high": 1, "default": 2, "low": 3}
PRIO_COLOR = {"urgent": 0xE74C3C, "high": 0xFF8C00,
              "low": 0x008000, "default": 0x7289DA}

WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
if not WEBHOOK:
    _wh_file = C.REPO / ".discord-webhook"
    if _wh_file.exists():
        WEBHOOK = _wh_file.read_text().strip()

BOT_NAME = os.environ.get("DISCORD_BOT_NAME", "MonkeForge Pipeline")
BOT_AVATAR = os.environ.get("DISCORD_BOT_AVATAR", "")

# --- Per-agent Discord identity -------------------------------------------
# Each agent role gets its own bot name and avatar when posting to Discord.
# Avatars are hardcoded URLs — replace with real images later.
_AVATAR_BASE = "https://raw.githubusercontent.com/Nocco-Matteo/MonkeForge/main/assets/avatars"

AGENT_IDENTITIES: dict[str, tuple[str, str]] = {
    "INTERVIEWER":       ("Curious Chimp",        f"{_AVATAR_BASE}/curious_chimp.png"),
    "PROPOSER":          ("Wise Orangutan",       f"{_AVATAR_BASE}/wise_orangutan.png"),
    "PLAN_REVIEWER":     ("Skeptical Baboon",     f"{_AVATAR_BASE}/skeptical_baboon.png"),
    "UX_REVIEWER":       ("Thoughtful Gibbon",    f"{_AVATAR_BASE}/thoughtful_gibbon.png"),
    "SUMMARIZER":        ("Concise Capuchin",     f"{_AVATAR_BASE}/concise_capuchin.png"),
    "JUDGE":             ("Stern Silverback",     f"{_AVATAR_BASE}/stern_silverback.png"),
    "IMPLEMENTER":       ("Diligent Drill",       f"{_AVATAR_BASE}/diligent_drill.png"),
    "CODE_REVIEWER":     ("Vigilant Vervet",      f"{_AVATAR_BASE}/vigilant_vervet.png"),
    "VISUAL_REVIEWER":   ("Observant Howler",     f"{_AVATAR_BASE}/observant_howler.png"),
    "VISUAL_FIXER":      ("Nimble Spider Monkey", f"{_AVATAR_BASE}/nimble_spider_monkey.png"),
    # System-level roles (pipeline orchestration, not a specific agent)
    "COUNCIL":           ("Monke Council",        f"{_AVATAR_BASE}/monke_council.png"),
    "ESCALATION":        ("Monke Council",        f"{_AVATAR_BASE}/monke_council.png"),
    "INTAKE":            ("Monke Council",        f"{_AVATAR_BASE}/monke_council.png"),
}

DEFAULT_IDENTITY = ("Monke Council", f"{_AVATAR_BASE}/monke_council.png")


def _identity_for(role: str) -> tuple[str, str]:
    """Return (username, avatar_url) for the given agent role."""
    return AGENT_IDENTITIES.get(role, DEFAULT_IDENTITY)


# --- Queue item ------------------------------------------------------------

_seq = 0


@dataclass(order=True)
class Item:
    sort_key: tuple
    title: str = field(compare=False)
    msg: str = field(compare=False)
    prio: str = field(compare=False)
    color: int = field(compare=False)
    role: str = field(compare=False, default="")


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def _enqueue(q: list[Item], title: str, msg: str, prio: str,
             role: str = "") -> None:
    color = PRIO_COLOR.get(prio, PRIO_COLOR["default"])
    key = (PRIO_ORDER.get(prio, 2), _next_seq())
    heapq.heappush(q, Item(key, title, msg, prio, color, role))


# --- Discord send ---------------------------------------------------------

def _send(title: str, msg: str, color: int, role: str = "") -> int:
    """POST to Discord webhook. Returns HTTP status code (204 = success)."""
    if not WEBHOOK:
        return 0
    username, avatar = _identity_for(role)
    payload = json.dumps({
        "username": username,
        "avatar_url": avatar,
        "embeds": [{"title": title[:256], "description": msg[:4000], "color": color}],
    })
    try:
        req = urllib.request.Request(
            WEBHOOK, data=payload.encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": "monkeforge-notify/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except (OSError, urllib.error.URLError):
        return 0


# --- Sliding window rate limiter -------------------------------------------

_send_times: list[float] = []


def _window_full() -> bool:
    now = time.time()
    cutoff = now - WINDOW
    while _send_times and _send_times[0] < cutoff:
        _send_times.pop(0)
    return len(_send_times) >= RATE


def _record_send() -> None:
    _send_times.append(time.time())


def _wait_until_slot() -> float:
    """Return seconds to wait until a slot opens in the window."""
    if not _window_full():
        return 0.0
    oldest = _send_times[0]
    return max(0.1, oldest + WINDOW - time.time())


# --- Persistence ----------------------------------------------------------

def _persist(q: list[Item]) -> None:
    try:
        data = [{"title": it.title, "msg": it.msg, "prio": it.prio,
                 "role": it.role}
                for it in sorted(q)]
        QUEUE_FILE.write_text(json.dumps(data))
    except OSError:
        pass


def _load() -> list[Item]:
    q: list[Item] = []
    try:
        if QUEUE_FILE.exists():
            for item in json.loads(QUEUE_FILE.read_text()):
                _enqueue(q, item["title"], item["msg"], item["prio"],
                         item.get("role", ""))
            QUEUE_FILE.unlink(missing_ok=True)
    except (OSError, ValueError):
        pass
    # Also drain any spool files left by events.py when the daemon was down.
    if SPOOL_DIR.is_dir():
        for f in sorted(SPOOL_DIR.glob("*.json")):
            try:
                rec = json.loads(f.read_text())
                _enqueue(q, rec["title"], rec["msg"], rec["prio"],
                         rec.get("role", ""))
                f.unlink()
            except (OSError, ValueError):
                pass
    return q


# --- Heartbeat -------------------------------------------------------------

def _heartbeat(queue_len: int) -> None:
    try:
        HEARTBEAT_FILE.write_text(json.dumps({
            "pid": os.getpid(),
            "queue": queue_len,
            "at": time.time(),
        }))
    except OSError:
        pass


# --- Logging ---------------------------------------------------------------

def _log(msg: str) -> None:
    try:
        with LOG_FILE.open("a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}\n")
    except OSError:
        pass


# --- Daemon loop ----------------------------------------------------------

_running = True


def _stop(*_):
    global _running
    _running = False


def run_daemon() -> int:
    global _running
    if not WEBHOOK:
        print("notify-daemon: no DISCORD_WEBHOOK configured — nothing to send.")
        return 1

    C.METRICS.mkdir(parents=True, exist_ok=True)
    # SPOOL_DIR may exist as a file (legacy notify.sh spool) — remove it first.
    if SPOOL_DIR.exists() and not SPOOL_DIR.is_dir():
        SPOOL_DIR.unlink()
    SPOOL_DIR.mkdir(parents=True, exist_ok=True)

    # Clean stale socket
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(SOCKET_PATH))
    srv.listen(64)
    srv.settimeout(1.0)
    _log(f"daemon started (pid {os.getpid()}, socket {SOCKET_PATH})")
    print(f"notify-daemon: listening on {SOCKET_PATH}")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    q = _load()
    _log(f"recovered {len(q)} queued item(s) from persistence/spool")

    while _running:
        # Drain the queue first
        while q and _running:
            if _window_full():
                wait = _wait_until_slot()
                _log(f"rate window full, waiting {wait:.1f}s")
                _heartbeat(len(q))
                time.sleep(min(wait, 5.0))
                continue

            item = heapq.heappop(q)
            code = _send(item.title, item.msg, item.color, item.role)

            if code == 204:
                _record_send()
                _log(f"http=204 prio={item.prio} | {item.title[:80]}")
            elif code == 429:
                _log("429 — retrying in 5s")
                heapq.heappush(q, item)
                _heartbeat(len(q))
                time.sleep(5)
                continue
            else:
                _log(f"http={code} prio={item.prio} | {item.title[:80]} — dropping")
                # Drop: don't re-queue a permanently failed send

            _heartbeat(len(q))

        # Accept new connections while queue is empty
        if _running:
            try:
                conn, _ = srv.accept()
                with conn:
                    data = b""
                    while True:
                        chunk = conn.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                        if len(data) > 65536:
                            break
                    if data:
                        try:
                            rec = json.loads(data)
                            _enqueue(q, rec["title"], rec["msg"],
                                     rec.get("prio", "default"),
                                     rec.get("role", ""))
                        except (ValueError, KeyError):
                            _log(f"malformed payload: {data[:200]}")
            except socket.timeout:
                pass
            except OSError:
                pass

        _heartbeat(len(q))

    # Shutdown: persist undelivered queue
    srv.close()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink(missing_ok=True)
    if q:
        _persist(q)
        _log(f"shutdown: persisted {len(q)} undelivered item(s)")
    else:
        QUEUE_FILE.unlink(missing_ok=True)
    _log("daemon stopped")
    print("notify-daemon: stopped")
    return 0


# --- Status / stop helpers -------------------------------------------------

def status() -> int:
    if not HEARTBEAT_FILE.exists():
        print("notify-daemon: not running (no heartbeat)")
        return 1
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
    except (OSError, ValueError):
        print("notify-daemon: heartbeat unreadable")
        return 1
    age = time.time() - hb.get("at", 0)
    pid = hb.get("pid")
    alive = False
    if pid:
        try:
            os.kill(pid, 0)
            alive = True
        except OSError:
            pass
    print(f"notify-daemon: {'alive' if alive else 'DEAD'} "
          f"(pid {pid}, queue {hb.get('queue', '?')}, "
          f"heartbeat {age:.0f}s ago)")
    return 0 if alive else 1


def stop() -> int:
    if not HEARTBEAT_FILE.exists():
        print("notify-daemon: not running")
        return 1
    try:
        hb = json.loads(HEARTBEAT_FILE.read_text())
        pid = hb.get("pid")
        if pid:
            os.kill(pid, signal.SIGTERM)
            print(f"notify-daemon: SIGTERM sent to pid {pid}")
            return 0
    except (OSError, ValueError):
        pass
    print("notify-daemon: could not stop (no pid in heartbeat)")
    return 1


if __name__ == "__main__":
    sys.exit(run_daemon())
