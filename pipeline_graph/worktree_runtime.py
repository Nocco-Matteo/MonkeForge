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
    if "PIPELINE_BRANCH_PREFIX" in os.environ:
        return os.environ["PIPELINE_BRANCH_PREFIX"]
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
    if repo_flag:
        p = Path(repo_flag).expanduser()
        if not p.is_absolute():
            p = (MF_ROOT / p).resolve()
        return _validate_repo(p)
    if "PIPELINE_REPO" in os.environ:
        env_val = os.environ["PIPELINE_REPO"]
        if env_val.strip():
            return _validate_repo(Path(env_val).expanduser().resolve())
        _fail(
            "PIPELINE_REPO is set but empty — set it to a non-empty path or pass --repo"
        )
    pl = _pipeline_dict()
    repo_raw = pl.get("repo")
    if repo_raw is not None and str(repo_raw).strip():
        return _validate_repo(Path(str(repo_raw)).expanduser().resolve())
    _fail(
        "no target repo configured — pass --repo PATH, set non-empty PIPELINE_REPO, "
        "or add pipeline.repo to monkeforge.yaml"
    )


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
            "absent (popping PIPELINE_DOCS_DIR from child_env loses to the "
            "child run.py's _load_yaml_to_env os.environ.setdefault)"
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


def prepare_task_isolation(
    id_: str,
    *,
    repo_flag: str | None = None,
    base: str = "main",
    brief_src: str | Path | None = None,
    create: bool = True,
) -> dict:
    """Create or reuse ``wt-task-{id}`` for a task run.

    ``create=True`` (start): create the worktree if missing; reuse if present.
    ``create=False`` (resume): require an existing worktree.

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
    src_path = Path(brief_src).expanduser() if brief_src else None
    try:
        brief_path = _copy_brief(target, id_, src=src_path)
    except SystemExit:
        if created or src_path is not None:
            raise
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


def reexec_isolated(info: dict, argv: list[str], *, run_py: Path | None = None) -> NoReturn:
    """Replace this process with ``run.py`` under the isolation child env."""
    env = isolation_child_env(info)
    script = str(run_py or (MF_ROOT / "run.py"))
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
        "graph", "land", "sync",
    }
    for i, a in enumerate(argv):
        if a in known:
            tid = None
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                tid = argv[i + 1]
            return a, tid
    return None, None


def bootstrap_run_isolation(argv: list[str], *, run_py: Path | None = None) -> None:
    """If ``start``/``resume`` with a task id, ensure wt and re-exec isolated.

    No-op when already isolated, ``--no-isolate`` present, or task id not yet
    known (start wizard path). Raises ``SystemExit`` on isolation errors.
    """
    if already_isolated() or "--no-isolate" in argv:
        return
    cmd, tid = early_cmd_and_task_id(argv)
    if cmd not in ("start", "resume") or not tid:
        return
    brief = early_argv_flag(argv, "file")
    repo = early_argv_flag(argv, "repo")
    info = prepare_task_isolation(
        tid,
        repo_flag=repo or None,
        brief_src=brief or None,
        create=(cmd == "start"),
    )
    reexec_isolated(info, argv, run_py=run_py)


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


def cmd_land(args) -> None:
    id_ = validate_id(args.id)
    target = resolve_target_repo(args.repo)
    prefix = resolve_branch_prefix()
    wt = wt_path_for(target, id_)
    branch = f"{prefix}{id_}"
    if not wt.exists() or not _branch_exists(target, branch):
        _fail(f"worktree/branch not found: {wt} / {branch}")
    status = _git(["status", "--porcelain"], cwd=target, check=False)
    if status.stdout.strip():
        _fail(f"target main is dirty — commit/stash on {target} before land")
    _git(["rebase", "main"], cwd=wt, check=True)
    if not getattr(args, "yes", False):
        try:
            ans = input(f"Land {branch} into main on {target}? [y/N] ").strip().lower()
        except EOFError:
            _fail("non-TTY confirm without -y — pass `-y/--yes` or run in a TTY")
        if ans not in ("y", "yes"):
            sys.stdout.write("aborted (no changes to main)\n")
            return
    _git(["checkout", "main"], cwd=target, check=True)
    merge = _git(["merge", "--ff-only", branch], cwd=target, check=False)
    if merge.returncode != 0:
        _fail(
            f"merge --ff-only failed — main moved since rebase. Re-run sync "
            f"or rebase {branch} onto main and retry. git output:\n"
            f"{merge.stderr.strip()}"
        )
    sys.stdout.write(f"landed {branch} → main on {target} (fast-forward)\n")


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
    sp.add_argument("-y", "--yes", action="store_true", help="skip the TTY confirm")
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
