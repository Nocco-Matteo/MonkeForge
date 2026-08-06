"""Worktree isolation runtime for MonkeForge (library + used by scripts/wt.py).

Creates/runs against an isolated git worktree of the TARGET product repo, with
docs/metrics isolated by worktree basename slug, Discord bot forced off for
parallel runs, and a child-process env contract that wins over the shared
``monkeforge.yaml`` via ``os.environ.setdefault``.

Import-safety: NO ``yaml.safe_load`` and NO ``pipeline_graph.config`` import at
module top — ``--help`` and early ``run.py`` bootstrap work with
``PIPELINE_REPO`` unset. Optional ``PIPELINE_WT_YAML`` overrides the yaml path
(default ``MF_ROOT/monkeforge.yaml``).

``<id>`` is the BARE task token (e.g. ``031``, ``smoke``), NOT the ``TASK-``
prefix: branch = ``{BRANCH_PREFIX}{id}``, worktree dir = ``wt-task-{id}``,
brief file = ``TASK-{id}-brief.md``.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

MF_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BRANCH_PREFIX = "feature/task-"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_WT_NAME_RE = re.compile(r"^wt-task-")
_E2E_URL_TEMPLATE = (
    "postgresql://postgres:postgrespassword@localhost:{port}/yourdb?schema=public"
)
_SUBCOMMANDS = (
    "ensure", "refresh-brief", "run", "overlap", "land", "sync", "list", "remove",
)
ISOLATED_ENV = "PIPELINE_ISOLATED"


class WorktreeError(Exception):
    """Operator-facing isolation/worktree failure (exit 2)."""

    def __init__(self, message: str, code: int = 2):
        super().__init__(message)
        self.message = message
        self.code = code


def _envstr(val) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    return str(val)


def _fail(msg: str, code: int = 2) -> NoReturn:
    """Abort with stderr + SystemExit (CLI + test contract)."""
    sys.stderr.write(f"wt: error: {msg}\n")
    raise SystemExit(code)


def _yaml_path() -> Path:
    return Path(os.environ.get("PIPELINE_WT_YAML") or (MF_ROOT / "monkeforge.yaml"))


def _load_yaml() -> dict:
    try:
        import yaml
    except ImportError:  # pragma: no cover
        return {}
    path = _yaml_path()
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (FileNotFoundError, PermissionError):
        return {}
    return data if isinstance(data, dict) else {}


def _pipeline_dict(yaml_data: dict | None = None) -> dict:
    if yaml_data is None:
        yaml_data = _load_yaml()
    pl = yaml_data.get("pipeline") if isinstance(yaml_data, dict) else None
    return pl if isinstance(pl, dict) else {}


def validate_id(id_: str) -> str:
    if not id_ or len(id_) > 64 or ".." in id_ or not _ID_RE.match(id_):
        _fail(
            f"invalid <id> {id_!r}: must match ^[A-Za-z0-9][A-Za-z0-9._-]*$, "
            f"contain no '..' or '/', and be <=64 chars"
        )
    return id_


def stable_hash(id_: str) -> int:
    return int(hashlib.sha256(id_.encode()).hexdigest(), 16)


def pool_port(id_: str) -> int:
    return 5434 + (stable_hash(id_) % 3)


def resolve_branch_prefix(yaml_data: dict | None = None) -> str:
    # yaml-only (§3c correction): stale PIPELINE_BRANCH_PREFIX env is ignored.
    pl = _pipeline_dict(yaml_data)
    if "branch_prefix" in pl:
        return _envstr(pl["branch_prefix"])
    return DEFAULT_BRANCH_PREFIX


def _validate_repo(path: Path) -> Path:
    path = path.expanduser().resolve()
    if not path.is_dir():
        _fail(f"target repo does not exist: {path}")
    if not (path / ".git").exists():
        _fail(f"target is not a git repo (missing .git): {path}")
    return path


def resolve_target_repo(repo_flag: str | None = None) -> Path:
    # Shared §3h resolution — delegates to repo_select.ensure_pipeline_repo so
    # run.py boot, bot/config.py, and the wt commands all follow ONE rule:
    # --repo > PIPELINE_REPO > repos: (1=auto, N=error non-interactive) >
    # pipeline.repo > fail. repo_select is import-light (no config import, yaml
    # loaded lazily), so the local import preserves the --help / early-bootstrap
    # contract (no top-level pipeline_graph import in this module). The
    # RepoSelectError → _fail conversion preserves this module's SystemExit(2)
    # contract that wt callers/tests expect.
    from pipeline_graph.repo_select import RepoSelectError, ensure_pipeline_repo
    try:
        return ensure_pipeline_repo(
            yaml_path=_yaml_path(),
            mf_root=MF_ROOT,
            repo_flag=repo_flag,
            interactive=False,
        )
    except RepoSelectError as exc:
        _fail(exc.cli_message())


def wt_path_for(target: Path, id_: str) -> Path:
    return target.parent / f"wt-task-{id_}"


def docs_dir_for(id_: str) -> Path:
    return MF_ROOT / "docs" / f"wt-task-{id_}"


def branch_for(id_: str, yaml_data: dict | None = None) -> str:
    return f"{resolve_branch_prefix(yaml_data)}{id_}"


def is_worktree_repo(path: Path | str | None = None) -> bool:
    p = Path(path or os.environ.get("PIPELINE_REPO", "")).resolve()
    return bool(_WT_NAME_RE.match(p.name))


def already_isolated() -> bool:
    return os.environ.get(ISOLATED_ENV) == "1" or is_worktree_repo()


def _git(args: list[str], cwd: Path | None = None, *, check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git"]
    if cwd is not None:
        cmd += ["-C", str(cwd)]
    cmd += args
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def live_worktrees(target: Path) -> list[dict]:
    out = _git(["worktree", "list", "--porcelain"], cwd=target, check=False)
    wts: list[dict] = []
    for block in out.stdout.split("\n\n"):
        if not block.strip():
            continue
        entry: dict = {}
        for line in block.splitlines():
            if " " in line:
                k, _, v = line.partition(" ")
                entry[k] = v
            elif line:
                entry[line] = ""
        path_raw = entry.get("worktree")
        if not path_raw:
            continue
        p = Path(path_raw)
        if p.exists():
            entry["path"] = p
            wts.append(entry)
    return wts


def live_feature_worktrees(target: Path) -> list[dict]:
    return [w for w in live_worktrees(target) if _WT_NAME_RE.match(w["path"].name)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_jsonl(path: Path, *, limit: int | None = None) -> list[dict]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    if limit is not None:
        lines = lines[-limit:]
    out: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _est_tokens_from_bytes(n: int) -> int:
    """Same ~4 chars/token heuristic as ``condenser.estimate_tokens``."""
    return max(0, int(n) // 4) if n else 0


def _fmt_duration(seconds: float) -> str:
    s = int(max(0, seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{sec:02d}s"
    return f"{sec}s"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _short_ts(ts: str) -> str:
    if "T" not in ts:
        return ts
    try:
        date, _, rest = ts.partition("T")
        return f"{date[5:]} {rest[:5]}"
    except Exception:
        return ts


def _parse_iso_ts(ts: str) -> float:
    if not ts:
        return 0.0
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _usage_stats(docs: Path, events: list[dict], *, task_id: str | None = None) -> dict:
    """Wall/active time + estimated prompt/completion tokens from on-disk logs.

    When ``task_id`` is set, prompt byte volume counts only files named
    ``{task_id}-*`` (avoids foreign ``smoke-*`` / other-task pollution).
    """
    metrics = docs / "metrics"
    starts = [e for e in events if e.get("kind") == "run_start"]
    ends = [e for e in events if e.get("kind") == "run_end"]
    t0 = 0.0
    if starts:
        t0 = _parse_iso_ts(str(starts[0].get("ts") or ""))
    elif events:
        t0 = _parse_iso_ts(str(events[0].get("ts") or ""))
    t1 = 0.0
    for e in events:
        t1 = max(t1, _parse_iso_ts(str(e.get("ts") or "")))
    if ends:
        t1 = max(t1, _parse_iso_ts(str(ends[-1].get("ts") or "")))
    wall_s = max(0.0, t1 - t0) if t0 else 0.0

    active_ms = 0
    agent_ms = 0
    for e in events:
        kind = e.get("kind")
        if kind == "step_end":
            active_ms += int(e.get("ms") or 0)
        elif kind == "agent_end":
            agent_ms += int(e.get("duration_ms") or 0)

    out_bytes = 0
    for rec in _read_jsonl(metrics / "runs.jsonl"):
        if rec.get("event") == "end":
            out_bytes += int(rec.get("output_bytes") or 0)
    if out_bytes == 0:
        raw = metrics / "raw"
        if raw.is_dir():
            for f in raw.iterdir():
                if f.is_file():
                    try:
                        out_bytes += f.stat().st_size
                    except OSError:
                        pass

    in_bytes = 0
    prompts = docs / "prompts"
    prefix = f"{task_id}-" if task_id else None
    if prompts.is_dir():
        for f in prompts.iterdir():
            if not f.is_file():
                continue
            if prefix is not None and not f.name.startswith(prefix):
                continue
            try:
                in_bytes += f.stat().st_size
            except OSError:
                pass

    tin = _est_tokens_from_bytes(in_bytes)
    tout = _est_tokens_from_bytes(out_bytes)
    return {
        "wall_s": wall_s,
        "active_s": active_ms / 1000.0,
        "agent_s": agent_ms / 1000.0,
        "tokens_in": tin,
        "tokens_out": tout,
        "wall": _fmt_duration(wall_s),
        "active": _fmt_duration(active_ms / 1000.0),
        "tokens": f"{_fmt_tokens(tin)}→{_fmt_tokens(tout)}",
    }


def worktree_run_summary(id_: str, *, wt: Path | None = None,
                         branch: str | None = None) -> dict:
    """Overview of an isolated task for ``./run.py status`` (no graph).

    Reads docs/metrics (current.json, events.jsonl, runs.jsonl, prompts/).
    """
    id_ = validate_id(id_)
    docs = docs_dir_for(id_)
    metrics = docs / "metrics"
    current_path = metrics / "current.json"
    events_path = metrics / "events.jsonl"

    cur: dict = {}
    if current_path.is_file():
        try:
            cur = json.loads(current_path.read_text(encoding="utf-8"))
            if not isinstance(cur, dict):
                cur = {}
        except Exception:
            cur = {}

    events = _read_jsonl(events_path)
    last_by: dict[str, dict] = {}
    latest_any: dict | None = None
    latest_any_ts = -1.0
    for ev in events:
        kind = str(ev.get("kind") or "")
        ts = _parse_iso_ts(str(ev.get("ts") or ""))
        if ts >= latest_any_ts:
            latest_any_ts = ts
            latest_any = ev
        if kind in ("run_end", "run_paused", "run_stalled", "run_start",
                    "escalation_open"):
            last_by[kind] = ev

    def _ev_ts(ev: dict | None) -> str:
        return str((ev or {}).get("ts") or "")

    def _ev_msg(ev: dict | None) -> str:
        return str((ev or {}).get("msg") or "")

    def _ev_step(ev: dict | None) -> str:
        return str((ev or {}).get("step") or "")

    end_ev = last_by.get("run_end")
    pause_ev = last_by.get("run_paused")
    stall_ev = last_by.get("run_stalled")
    start_ev = last_by.get("run_start")

    pid = cur.get("pid")
    try:
        pid_i = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid_i = None
    alive = bool(pid_i is not None and _pid_alive(pid_i))

    state = "unknown"
    detail = ""
    pause_ts = ""

    if end_ev and (
        not pause_ev or _ev_ts(end_ev) >= _ev_ts(pause_ev)
    ) and (
        not stall_ev or _ev_ts(end_ev) >= _ev_ts(stall_ev)
    ):
        state = "finished"
        detail = _ev_msg(end_ev) or "run finished"
    elif cur.get("idle") and cur.get("why") == "finished":
        state = "finished"
        detail = "run finished"
    elif pause_ev and (
        not end_ev or _ev_ts(pause_ev) > _ev_ts(end_ev)
    ):
        state = "paused"
        detail = _ev_msg(pause_ev) or f"paused at {_ev_step(pause_ev) or '?'}"
        pause_ts = _ev_ts(pause_ev)
    elif cur.get("idle") and cur.get("why") == "paused":
        state = "paused"
        detail = "paused"
        pause_ts = str(cur.get("at") or "")
    elif stall_ev:
        state = "stalled"
        detail = _ev_msg(stall_ev) or "driver stalled"
    elif alive:
        state = "running"
        detail = str(cur.get("step") or _ev_step(start_ev) or "in progress")
        if cur.get("agent"):
            detail = f"{detail} ({cur.get('agent')})"
    elif pid_i is not None and not alive:
        state = "dead"
        detail = (
            f"process pid {pid_i} gone"
            + (f"; last step {cur.get('step')}" if cur.get("step") else "")
        )
    elif not current_path.exists() and not events_path.exists():
        state = "idle"
        detail = "no metrics yet"
    else:
        state = "idle"
        detail = str(cur.get("why") or "no active run")

    if state == "paused" and pause_ev:
        stage = _ev_step(pause_ev)
        msg = _ev_msg(pause_ev)
        if stage and msg:
            detail = f"{stage}: {msg}"
        elif msg:
            detail = msg
        elif stage:
            detail = f"paused at {stage}"
        if stall_ev and _ev_ts(stall_ev) > _ev_ts(pause_ev) and not alive:
            detail = f"{detail} (driver died after pause)"
    elif state == "finished" and detail:
        for sep in (" — repo=", " — land:", " — "):
            if sep in detail:
                detail = detail.split(sep, 1)[0].strip()
                break
        if "land" not in detail.lower():
            detail = f"{detail} · land when ready"

    detail = " ".join(detail.split())
    if len(detail) > 64:
        detail = detail[:61] + "…"

    br = branch or ""
    if br.startswith("refs/heads/"):
        br = br[len("refs/heads/"):]

    activity_candidates = [
        _ev_ts(latest_any),
        _ev_ts(stall_ev),
        _ev_ts(end_ev),
        _ev_ts(pause_ev),
        str(cur.get("heartbeat") or ""),
        str(cur.get("at") or ""),
        pause_ts,
    ]
    activity_ts = max(activity_candidates, key=_parse_iso_ts) if any(
        activity_candidates) else ""

    usage = _usage_stats(docs, events, task_id=id_)

    return {
        "id": id_,
        "state": state,
        "detail": detail,
        "step": str(cur.get("step") or _ev_step(pause_ev) or _ev_step(start_ev) or ""),
        "updated": _short_ts(activity_ts),
        "paused_at": _short_ts(pause_ts) if pause_ts else "",
        "branch": br,
        "path": str(wt) if wt is not None else "",
        "docs": str(docs),
        "pid": pid_i,
        "alive": alive,
        "wall": usage["wall"],
        "active": usage["active"],
        "wall_s": usage["wall_s"],
        "active_s": usage["active_s"],
        "agent_s": usage["agent_s"],
        "tokens_in": usage["tokens_in"],
        "tokens_out": usage["tokens_out"],
        "tokens": usage["tokens"],
    }


def _branch_exists(target: Path, branch: str) -> bool:
    out = _git(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
               cwd=target, check=False)
    return out.returncode == 0


def _docs_dir_banned_paths(target: Path, live_wts: list[dict]) -> set[Path]:
    banned = {(MF_ROOT / "docs" / target.name).resolve()}
    for w in live_wts:
        name = w["path"].name
        if _WT_NAME_RE.match(name):
            banned.add((MF_ROOT / "docs" / name).resolve())
    return banned


def _check_docs_dir_ban(target: Path, live_wts: list[dict]) -> None:
    docs_env = os.environ.get("PIPELINE_DOCS_DIR")
    if not docs_env:
        return
    resolved = Path(docs_env).expanduser().resolve()
    if resolved in _docs_dir_banned_paths(target, live_wts):
        _fail(
            f"PIPELINE_DOCS_DIR {resolved} is a shared docs path (canonical or "
            f"another live slug) — unset it; isolation derives docs from the "
            f"worktree basename automatically"
        )


def _check_yaml_docs_dir_absent() -> None:
    pl = _pipeline_dict()
    if "docs_dir" in pl:
        _fail(
            "monkeforge.yaml has pipeline.docs_dir — isolation requires it "
            "absent (the isolated child pops PIPELINE_DOCS_DIR from its env so "
            "config.py falls back to the per-worktree docs/<wt-slug>/ dir; a "
            "yaml docs_dir would override that fallback and point every "
            "isolated run at the same shared docs tree)"
        )


def _brief_paths(target: Path, id_: str) -> tuple[Path, Path]:
    src = MF_ROOT / "docs" / target.name / "tasks" / f"TASK-{id_}-brief.md"
    dst = docs_dir_for(id_) / "tasks" / f"TASK-{id_}-brief.md"
    return src, dst


def _copy_brief(target: Path, id_: str, *, src: Path | None = None) -> Path:
    canonical, dst = _brief_paths(target, id_)
    source = src if src is not None else canonical
    source = source.expanduser().resolve()
    if not source.exists():
        # Isolation keeps the live brief under docs/wt-task-*/. On resume the
        # canonical docs/<product>/tasks/ copy is often absent (task started
        # with --file, or never mirrored there) — reuse dst quietly instead of
        # spamming ``wt: error: brief source missing``.
        if src is None and dst.exists():
            return dst
        _fail(f"brief source missing: {source}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, dst)
    return dst


def build_child_env(wt_path: Path, port: int, parent_env: dict | None = None) -> dict:
    env = dict(parent_env if parent_env is not None else os.environ)
    env["PIPELINE_REPO"] = str(wt_path.resolve())
    env["PIPELINE_BOT_AUTOSTART"] = "0"
    env.pop("PIPELINE_DOCS_DIR", None)
    env[ISOLATED_ENV] = "1"
    # ``Popen(cwd=…)`` changes the real cwd but leaves ``PWD`` inherited from
    # the orchestrator, which runs in MF_ROOT. Agent CLIs that root their
    # workspace on ``$PWD`` rather than ``getcwd()`` then write into MF_ROOT
    # while cwd is correctly the worktree — the TASK-032 write-escape.
    env["PWD"] = str(wt_path.resolve())
    env.pop("OLDPWD", None)
    # Orchestrator yaml stays outside the product worktree. Child processes
    # (esp. self-host pytest importing pipeline_graph.config from the wt)
    # must resolve agents/config via this path — never by copying yaml into wt.
    yaml_p = _yaml_path()
    if yaml_p.exists():
        env.setdefault("PIPELINE_WT_YAML", str(yaml_p.resolve()))
    has_port = "PIPELINE_E2E_DB_PORT" in env
    has_url = bool(env.get("PIPELINE_E2E_DATABASE_URL", "").strip())
    if not (has_port and has_url):
        env["PIPELINE_E2E_DB_PORT"] = str(port)
        env["PIPELINE_E2E_DATABASE_URL"] = _E2E_URL_TEMPLATE.format(port=port)
    return env


def assert_child_env(child_env: dict, target: Path) -> None:
    repo = child_env.get("PIPELINE_REPO")
    if not repo:
        _fail("pre-exec assert: PIPELINE_REPO missing from child env")
    p = Path(repo).resolve()
    if not _WT_NAME_RE.match(p.name):
        _fail(
            f"pre-exec assert: PIPELINE_REPO basename {p.name!r} does not match "
            f"^wt-task-"
        )
    if not p.exists():
        _fail(f"pre-exec assert: worktree path does not exist: {p}")
    live = {w["path"].resolve(): w for w in live_worktrees(target)}
    if p not in live:
        _fail(f"pre-exec assert: {p} is not a live worktree of {target}")
    # A stale PWD pointing at MF_ROOT is what let an agent write onto the
    # orchestrator checkout while its cwd was correctly the worktree.
    pwd = child_env.get("PWD")
    if pwd and Path(pwd).resolve() != p:
        _fail(
            f"pre-exec assert: PWD {pwd!r} disagrees with PIPELINE_REPO {str(p)!r}"
        )


def prepare_task_isolation(
    id_: str,
    *,
    repo_flag: str | None = None,
    base: str = "main",
    brief_src: str | Path | None = None,
    create: bool = True,
    copy_brief: bool = True,
) -> dict:
    """Create or reuse ``wt-task-{id}`` for a task run.

    ``create=True`` (start): create the worktree if missing; reuse if present.
    ``create=False`` (resume): require an existing worktree.
    ``copy_brief=False`` (status/doctor/…): skip brief refresh entirely.

    Returns dict with target/wt/branch/docs/brief/created/live_feature_count.
    """
    id_ = validate_id(id_)
    target = resolve_target_repo(repo_flag)
    if _WT_NAME_RE.match(target.name):
        _fail(
            f"target repo looks like a task worktree ({target.name}). "
            f"Pass the product repo (e.g. MonkeForge), not wt-task-*"
        )

    prefix = resolve_branch_prefix()
    wt = wt_path_for(target, id_)
    branch = f"{prefix}{id_}"
    live = live_worktrees(target)
    _check_docs_dir_ban(target, live)
    _check_yaml_docs_dir_absent()

    created = False
    wt_live = any(w["path"].resolve() == wt.resolve() for w in live)
    if wt_live or wt.exists():
        if not wt.exists():
            _fail(f"worktree metadata present but path missing: {wt}")
        created = False
    elif create:
        if _branch_exists(target, branch):
            _fail(
                f"branch collision: {branch} already exists — run "
                f"`./run.py land {id_}` / `scripts/wt.py remove {id_}` "
                f"or `git branch -D {branch}` first"
            )
        _git(["worktree", "add", "-b", branch, str(wt), base], cwd=target, check=True)
        created = True
    else:
        _fail(
            f"worktree does not exist: {wt} — start the task first "
            f"(`./run.py start {id_} …`)"
        )

    brief_path = None
    if copy_brief:
        src_path = Path(brief_src).expanduser() if brief_src else None
        try:
            brief_path = _copy_brief(target, id_, src=src_path)
        except SystemExit:
            if created or src_path is not None:
                raise
            _, dst = _brief_paths(target, id_)
            brief_path = dst if dst.exists() else None
    else:
        _, dst = _brief_paths(target, id_)
        brief_path = dst if dst.exists() else None

    live_feat = live_feature_worktrees(target)
    return {
        "target": target,
        "wt": wt.resolve(),
        "branch": branch,
        "docs": docs_dir_for(id_),
        "brief": brief_path,
        "created": created,
        "live_feature_count": len(live_feat),
        "id": id_,
    }


def format_isolation_banner(info: dict) -> str:
    n = info.get("live_feature_count", 1)
    created = "created" if info.get("created") else "reused"
    return (
        f"task {info['id']} ready ({created})\n"
        f"  code:    {info['wt']}\n"
        f"  docs:    {info['docs']}\n"
        f"  branch:  {info['branch']}\n"
        f"  target:  {info['target']}\n"
        f"  parallel: {n} live feature worktree(s); bot: webhook-only\n"
    )


def isolation_child_env(info: dict, parent_env: dict | None = None) -> dict:
    port = pool_port(info["id"])
    env = build_child_env(info["wt"], port, parent_env=parent_env)
    assert_child_env(env, info["target"])
    return env


def reexec_isolated(info: dict, argv: list[str], *, run_py: Path | None = None,
                    quiet: bool = False) -> NoReturn:
    """Replace this process with ``run.py`` under the isolation child env."""
    env = isolation_child_env(info)
    script = str(run_py or (MF_ROOT / "run.py"))
    if not quiet:
        sys.stderr.write(format_isolation_banner(info))
        sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, script, *argv], env)


def early_argv_flag(argv: list[str], name: str) -> str | None:
    """Pull ``--name`` / ``--name=`` from argv before full argparse."""
    long = f"--{name}"
    for i, a in enumerate(argv):
        if a == long:
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                return argv[i + 1]
            return ""
        if a.startswith(long + "="):
            return a.split("=", 1)[1]
    return None


def early_cmd_and_task_id(argv: list[str]) -> tuple[str | None, str | None]:
    """Find ``start|resume|land|…`` and the following bare task id, if any."""
    known = {
        "start", "resume", "redo", "reset", "status", "doctor", "metrics",
        "graph", "land", "sync", "eyes",
    }
    for i, a in enumerate(argv):
        if a in known:
            tid = None
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                tid = argv[i + 1]
            return a, tid
    return None, None


def bootstrap_run_isolation(argv: list[str], *, run_py: Path | None = None) -> None:
    """If a task-scoped command has a task id, ensure wt and re-exec isolated.

    ``start`` create-or-reuse; ``resume``/``eyes``/``status``/``doctor``/
    ``metrics``/``reset``/``redo`` reuse only (so status reads
    ``docs/wt-task-*``, not the product basename docs). No-op when already
    isolated, ``--no-isolate``, or task id not yet known (start wizard / bare
    ``status`` list).
    """
    if already_isolated() or "--no-isolate" in argv:
        return
    cmd, tid = early_cmd_and_task_id(argv)
    if not tid:
        return
    create_cmds = ("start",)
    reuse_cmds = ("resume", "eyes", "status", "doctor", "metrics", "reset", "redo")
    if cmd not in create_cmds and cmd not in reuse_cmds:
        return
    brief = early_argv_flag(argv, "file")
    repo = early_argv_flag(argv, "repo")
    # status/doctor/metrics: don't refresh briefs (and don't stderr on missing
    # canonical source). start/resume/redo/reset keep the copy behaviour.
    copy_brief = cmd not in ("status", "doctor", "metrics")
    info = prepare_task_isolation(
        tid,
        repo_flag=repo or None,
        brief_src=brief or None,
        create=(cmd in create_cmds),
        copy_brief=copy_brief,
    )
    reexec_isolated(info, argv, run_py=run_py, quiet=(cmd == "status"))


# --------------------------------------------------------------------------- #
# CLI subcommands (scripts/wt.py + ./run.py land)
# --------------------------------------------------------------------------- #
def cmd_ensure(args) -> None:
    """Strict ensure for the workshop CLI — fails on slug collision (reuse is
    ``prepare_task_isolation`` / ``./run.py start``)."""
    id_ = validate_id(args.id)
    target = resolve_target_repo(args.repo)
    wt = wt_path_for(target, id_)
    live = live_worktrees(target)
    if any(w["path"].resolve() == wt.resolve() for w in live) or wt.exists():
        _fail(f"slug collision: live worktree already at {wt}")
    info = prepare_task_isolation(
        id_,
        repo_flag=getattr(args, "repo", None),
        base=getattr(args, "base", None) or "main",
        create=True,
    )
    sys.stdout.write(
        f"created worktree {info['wt']} on branch {info['branch']}\n"
        f"arch_docs note: paths are repo-relative to the wt; renamed main docs "
        f"need `wt sync`.\n"
    )


def cmd_refresh_brief(args) -> None:
    id_ = validate_id(args.id)
    target = resolve_target_repo(args.repo)
    dst = _copy_brief(target, id_)
    sys.stdout.write(f"refreshed brief → {dst}\n")


def cmd_run(args) -> None:
    id_ = validate_id(args.id)
    target = resolve_target_repo(args.repo)
    wt = wt_path_for(target, id_)
    if not wt.exists():
        _fail(f"worktree does not exist: {wt} — run `wt ensure {id_}` first")
    live = live_worktrees(target)
    _check_docs_dir_ban(target, live)
    _check_yaml_docs_dir_absent()
    port = pool_port(id_)
    child_env = build_child_env(wt, port)
    assert_child_env(child_env, target)
    if args.notify_level is not None:
        child_env["PIPELINE_NOTIFY_LEVEL"] = args.notify_level
    cmd = [c for c in args.cmd if c]
    if not cmd:
        _fail("wt run: no command given after --")
    cwd = Path(args.cwd).resolve() if args.cwd else MF_ROOT
    proc = subprocess.run(cmd, cwd=str(cwd), env=child_env)
    raise SystemExit(proc.returncode)


def _overlap_changed_files(wt: Path) -> list[str]:
    out = subprocess.run(
        f"git -C {wt} diff --name-only $(git -C {wt} merge-base HEAD main)...HEAD",
        shell=True, capture_output=True, text=True, check=False,
    )
    return [f for f in out.stdout.splitlines() if f.strip()]


def cmd_overlap(args) -> None:
    target = resolve_target_repo(args.repo)
    live = live_worktrees(target)
    files: dict[str, set[str]] = {}
    for w in live:
        name = w["path"].name
        if not _WT_NAME_RE.match(name):
            continue
        files[name] = set(_overlap_changed_files(w["path"]))
    names = list(files)
    any_intersection = False
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            inter = files[names[i]] & files[names[j]]
            if inter:
                any_intersection = True
                sys.stdout.write(
                    f"{names[i]} ∩ {names[j]}: {len(inter)} overlapping file(s)\n"
                )
                for f in sorted(inter):
                    sys.stdout.write(f"  {f}\n")
    if not any_intersection:
        sys.stdout.write("no overlapping file changes across live feature worktrees\n")


def land_to_main(*, id: str, repo: str | None = None) -> dict:
    """Rebase feature wt onto main and ff-only merge into target main.

    No interactive confirm — invoking land *is* the operator intent.
    Returns ``{id, target, wt, branch}``. Does not remove the worktree.
    """
    id_ = validate_id(id)
    target = resolve_target_repo(repo)
    prefix = resolve_branch_prefix()
    wt = wt_path_for(target, id_)
    branch = f"{prefix}{id_}"
    if not wt.exists() or not _branch_exists(target, branch):
        _fail(f"worktree/branch not found: {wt} / {branch}")
    status = _git(["status", "--porcelain"], cwd=target, check=False)
    if status.stdout.strip():
        _fail(f"target main is dirty — commit/stash on {target} before land")
    rb = _git(["rebase", "main"], cwd=wt, check=False)
    if rb.returncode != 0:
        detail = (rb.stderr or rb.stdout or "").strip() or f"exit {rb.returncode}"
        _fail(
            f"rebase onto main failed for {branch} in {wt}.\n"
            f"Resolve conflicts (or abort with `git -C {wt} rebase --abort`), "
            f"then re-run land.\n"
            f"git output:\n{detail}"
        )
    _git(["checkout", "main"], cwd=target, check=True)
    merge = _git(["merge", "--ff-only", branch], cwd=target, check=False)
    if merge.returncode != 0:
        _fail(
            f"merge --ff-only failed — main moved since rebase. Re-run sync "
            f"or rebase {branch} onto main and retry. git output:\n"
            f"{merge.stderr.strip()}"
        )
    return {"id": id_, "target": target, "wt": wt, "branch": branch}


def cleanup_landed_worktree(*, target: Path, id_: str, wt: Path, branch: str) -> None:
    """Remove worktree + delete feature branch after land. Docs stay."""
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=target, check=False)
    _git(["worktree", "prune"], cwd=target, check=False)
    rm = _git(["branch", "-D", branch], cwd=target, check=False)
    if rm.returncode != 0:
        _fail(
            f"landed, but could not delete branch {branch}: "
            f"{rm.stderr.strip() or rm.stdout.strip()}\n"
            f"  Worktree may already be gone; delete the branch manually if needed."
        )


def cmd_land(args) -> None:
    """CLI entry for ``wt land`` / thin wrapper used by tests.

    Lands immediately (no pre-merge confirm). Cleanup ask stays here for the
    plain ``wt`` CLI; ``./run.py land`` handles Rich UX in ``run.py``.
    """
    info = land_to_main(id=args.id, repo=getattr(args, "repo", None))
    id_ = info["id"]
    target = info["target"]
    wt = info["wt"]
    branch = info["branch"]
    sys.stdout.write(f"landed {branch} → main on {target}\n")
    if getattr(args, "run_py_ux", False):
        # Caller (run.py) owns cleanup prompt + messages.
        return
    _land_cleanup(args, target=target, id_=id_, wt=wt, branch=branch)


def _land_cleanup(args, *, target: Path, id_: str, wt: Path, branch: str) -> None:
    """Optionally remove worktree + feature branch after a successful land.

    Keeps ``docs/wt-task-*`` always. Default on a TTY: ask, Y to remove.
    ``--cleanup`` / ``--yes`` removes without asking; ``--keep-worktree`` never.
    Non-TTY without flags: keep and print a hint.
    """
    if getattr(args, "keep_worktree", False):
        sys.stdout.write(
            f"kept worktree {wt} and branch {branch} (--keep-worktree); "
            f"docs stay at docs/wt-task-{id_}\n"
        )
        return
    do_cleanup = bool(getattr(args, "cleanup", False) or getattr(args, "yes", False))
    if not do_cleanup:
        if sys.stdin.isatty():
            try:
                ans = input(
                    f"Remove worktree + delete {branch}? "
                    f"(docs/wt-task-{id_} kept) [Y/n] "
                ).strip().lower()
            except EOFError:
                ans = "n"
            do_cleanup = ans in ("", "y", "yes")
        else:
            sys.stdout.write(
                f"kept worktree {wt} + {branch} (non-TTY; "
                f"pass --cleanup to remove, or --keep-worktree to silence)\n"
            )
            return
    if not do_cleanup:
        sys.stdout.write(f"kept worktree {wt} and branch {branch}\n")
        return
    cleanup_landed_worktree(target=target, id_=id_, wt=wt, branch=branch)
    sys.stdout.write(
        f"removed worktree + branch {branch} (docs/wt-task-{id_} kept)\n"
    )


def cmd_sync(args) -> None:
    target = resolve_target_repo(args.repo)
    live = live_worktrees(target)
    n = 0
    for w in live:
        if not _WT_NAME_RE.match(w["path"].name):
            continue
        _git(["rebase", "main"], cwd=w["path"], check=True)
        sys.stdout.write(f"rebased {w['path'].name} onto main\n")
        n += 1
    if not n:
        sys.stdout.write("no live feature worktrees to sync\n")


def cmd_list(args) -> None:
    target = resolve_target_repo(args.repo)
    live = live_worktrees(target)
    if not live:
        sys.stdout.write("(no live worktrees)\n")
        return
    for w in live:
        branch = w.get("branch") or "(detached)"
        sys.stdout.write(f"{w['path']}\t{branch}\n")


def cmd_remove(args) -> None:
    id_ = validate_id(args.id)
    target = resolve_target_repo(args.repo)
    prefix = resolve_branch_prefix()
    wt = wt_path_for(target, id_)
    branch = f"{prefix}{id_}"
    if wt.exists():
        _git(["worktree", "remove", "--force", str(wt)], cwd=target, check=False)
    _git(["worktree", "prune"], cwd=target, check=False)
    rm = _git(["branch", "-D", branch], cwd=target, check=False)
    if rm.returncode != 0:
        _fail(
            f"could not delete branch {branch}: {rm.stderr.strip() or rm.stdout.strip()}\n"
            f"  If it is checked out elsewhere, switch that worktree off the "
            f"branch first, then re-run remove."
        )
    sys.stdout.write(f"removed worktree + branch {branch}\n")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="wt",
        description=(
            "git-worktree parallelism CLI (workshop). Operators normally use "
            "./run.py start/resume/land — this CLI shares the same runtime."
        ),
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_repo(sp):
        sp.add_argument(
            "--repo",
            help="target product repo path (default: PIPELINE_REPO / yaml pipeline.repo)",
        )

    sp = sub.add_parser("ensure", help="create a worktree + branch + brief copy")
    add_repo(sp)
    sp.add_argument("--base", default="main", help="base ref to branch from (default main)")
    sp.add_argument("id")
    sp.set_defaults(func=cmd_ensure)

    sp = sub.add_parser("refresh-brief", help="re-copy the brief from canonical docs")
    add_repo(sp)
    sp.add_argument("id")
    sp.set_defaults(func=cmd_refresh_brief)

    sp = sub.add_parser("run", help="run a command in the worktree's child env")
    add_repo(sp)
    sp.add_argument("--notify-level", default=None, help="set PIPELINE_NOTIFY_LEVEL")
    sp.add_argument("--cwd", default=None, help="cwd for the command (default MF_ROOT)")
    sp.add_argument("cmd", nargs=argparse.REMAINDER, help="command after --")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("overlap", help="print pairwise file overlaps across live feature wts")
    add_repo(sp)
    sp.set_defaults(func=cmd_overlap)

    sp = sub.add_parser("land", help="rebase wt onto main + ff-only merge into target main")
    add_repo(sp)
    sp.add_argument(
        "-y", "--yes", action="store_true",
        help="after land, remove worktree + feature branch without asking",
    )
    sp.add_argument(
        "--cleanup",
        action="store_true",
        help="after land, remove worktree + delete feature branch (keep docs)",
    )
    sp.add_argument(
        "--keep-worktree",
        action="store_true",
        help="after land, do not ask / remove worktree or feature branch",
    )
    sp.add_argument("id")
    sp.set_defaults(func=cmd_land)

    sp = sub.add_parser("sync", help="rebase all live feature wts onto main")
    add_repo(sp)
    sp.set_defaults(func=cmd_sync)

    sp = sub.add_parser("list", help="list live worktrees of the target")
    add_repo(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("remove", help="remove worktree + branch (survives manual dir delete)")
    add_repo(sp)
    sp.add_argument("id")
    sp.set_defaults(func=cmd_remove)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
