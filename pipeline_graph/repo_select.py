"""Resolve the target git repo before ``config.REPO`` is fixed.

Precedence: ``--repo`` flag > ``PIPELINE_REPO`` env > yaml ``repos:``
(1 entry = auto, N = CLI picker). No git-cwd / MF_ROOT fallback.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RepoEntry:
    path: Path
    label: str


class RepoSelectError(Exception):
    """Expected operator mistake resolving the target repo (no traceback)."""

    def __init__(self, message: str):
        self._message = message
        super().__init__(message)

    def cli_message(self) -> str:
        return self._message


def _cli_error(lines: list[str]) -> RepoSelectError:
    return RepoSelectError("\n".join(lines))


def load_repos(yaml_path: Path, *, mf_root: Path) -> list[RepoEntry]:
    """Parse top-level ``repos:`` from monkeforge.yaml. Missing file → []."""
    if not yaml_path.is_file():
        return []
    try:
        import yaml as _yaml
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except (OSError, Exception):
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("repos")
    if not isinstance(raw, list):
        return []
    out: list[RepoEntry] = []
    for item in raw:
        if isinstance(item, str):
            p = Path(item).expanduser()
            if not p.is_absolute():
                p = (mf_root / p).resolve()
            else:
                p = p.resolve()
            out.append(RepoEntry(path=p, label=p.name))
        elif isinstance(item, dict):
            path_raw = item.get("path")
            if not path_raw:
                continue
            p = Path(str(path_raw)).expanduser()
            if not p.is_absolute():
                p = (mf_root / p).resolve()
            else:
                p = p.resolve()
            label = str(item.get("label") or p.name).strip() or p.name
            out.append(RepoEntry(path=p, label=label))
    return out


def validate_repo(path: Path) -> Path:
    """Require an existing directory with a ``.git`` entry."""
    path = path.expanduser().resolve()
    if not path.is_dir():
        raise _cli_error([
            f"error: target repo does not exist: {path}",
            "",
            "  Set PIPELINE_REPO / --repo to a git checkout, or fix repos: in",
            "  monkeforge.yaml.",
        ])
    if not (path / ".git").exists():
        raise _cli_error([
            f"error: target is not a git repo (missing .git): {path}",
            "",
            "  Every target must be a git repository (git init + commit).",
        ])
    return path


def resolve_repo_flag(flag: str, repos: list[RepoEntry], *, mf_root: Path) -> Path:
    """Resolve ``--repo`` as 1-based index, label, or filesystem path."""
    flag = flag.strip()
    if not flag:
        raise _cli_error(["error: --repo is empty"])
    if flag.isdigit():
        idx = int(flag)
        if idx < 1 or idx > len(repos):
            raise _cli_error([
                f"error: --repo {flag} is out of range "
                f"(repos: has {len(repos)} entr{'y' if len(repos) == 1 else 'ies'})",
            ])
        return validate_repo(repos[idx - 1].path)
    for r in repos:
        if r.label == flag:
            return validate_repo(r.path)
    p = Path(flag).expanduser()
    if not p.is_absolute():
        # Try relative to mf_root, then cwd.
        cand = (mf_root / p).resolve()
        if cand.is_dir():
            return validate_repo(cand)
        cand = p.resolve()
        return validate_repo(cand)
    return validate_repo(p)


def pick_repo_interactive(repos: list[RepoEntry]) -> Path:
    """Numbered picker on stderr; read index from stdin."""
    sys.stderr.write("Select target repo:\n")
    for i, r in enumerate(repos, 1):
        sys.stderr.write(f"  {i}. {r.label}  ({r.path})\n")
    sys.stderr.write("> ")
    sys.stderr.flush()
    try:
        line = sys.stdin.readline()
    except EOFError as exc:
        raise _cli_error([
            "error: no repo selected (EOF on stdin)",
            "",
            "  Pass --repo <label|path|index> or set PIPELINE_REPO.",
        ]) from exc
    if not line:
        raise _cli_error([
            "error: no repo selected (EOF on stdin)",
            "",
            "  Pass --repo <label|path|index> or set PIPELINE_REPO.",
        ])
    choice = line.strip()
    if not choice.isdigit():
        raise _cli_error([
            f"error: expected a number 1..{len(repos)}, got {choice!r}",
        ])
    idx = int(choice)
    if idx < 1 or idx > len(repos):
        raise _cli_error([
            f"error: {idx} is out of range (1..{len(repos)})",
        ])
    return validate_repo(repos[idx - 1].path)


def _yaml_pipeline_repo(yaml_path: Path) -> str | None:
    """The ``pipeline.repo`` scalar from monkeforge.yaml, or ``None``."""
    if not yaml_path.is_file():
        return None
    try:
        import yaml as _yaml
        data = _yaml.safe_load(yaml_path.read_text()) or {}
    except (OSError, Exception):
        return None
    if not isinstance(data, dict):
        return None
    pl = data.get("pipeline")
    if not isinstance(pl, dict):
        return None
    raw = pl.get("repo")
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def ensure_pipeline_repo(
    *,
    yaml_path: Path,
    mf_root: Path,
    repo_flag: str | None = None,
    interactive: bool = True,
) -> Path:
    """Ensure ``PIPELINE_REPO`` is set; return the resolved path.

    Precedence: ``--repo`` flag > ``PIPELINE_REPO`` env > yaml ``repos:``
    (1 entry = auto, N = CLI picker) > yaml ``pipeline.repo`` scalar.
    Side effect: writes ``os.environ["PIPELINE_REPO"]``.
    """
    repos = load_repos(yaml_path, mf_root=mf_root)

    if repo_flag:
        path = resolve_repo_flag(repo_flag, repos, mf_root=mf_root)
        os.environ["PIPELINE_REPO"] = str(path)
        return path

    env = os.environ.get("PIPELINE_REPO", "").strip()
    if env:
        path = validate_repo(Path(env))
        os.environ["PIPELINE_REPO"] = str(path)
        return path

    if not repos:
        # yaml pipeline.repo scalar fallback (single-repo shortcut: no repos:
        # list needed). Resolved relative to mf_root when not absolute.
        repo_scalar = _yaml_pipeline_repo(yaml_path)
        if repo_scalar:
            p = Path(repo_scalar).expanduser()
            if not p.is_absolute():
                p = (mf_root / p).resolve()
            else:
                p = p.resolve()
            path = validate_repo(p)
            os.environ["PIPELINE_REPO"] = str(path)
            sys.stderr.write(f"using repo: {path.name}  ({path})\n")
            sys.stderr.flush()
            return path
        raise _cli_error([
            "error: no target repo configured",
            "",
            "  There is no default (cwd / git root is NOT used).",
            "  Do one of:",
            "    • export PIPELINE_REPO=/abs/path/to/app",
            "    • ./run.py --repo <label|path|index> …",
            "    • add a top-level `repos:` list or a `pipeline.repo` scalar",
            "      to monkeforge.yaml",
            "",
            f"  yaml: {yaml_path}",
        ])

    if len(repos) == 1:
        path = validate_repo(repos[0].path)
        os.environ["PIPELINE_REPO"] = str(path)
        sys.stderr.write(f"using repo: {repos[0].label}  ({path})\n")
        sys.stderr.flush()
        return path

    if not interactive or not sys.stdin.isatty():
        labels = ", ".join(f"{i}:{r.label}" for i, r in enumerate(repos, 1))
        raise _cli_error([
            f"error: {len(repos)} repos in monkeforge.yaml; choose one",
            "",
            f"  Available: {labels}",
            "  Pass --repo <label|path|index> or set PIPELINE_REPO.",
            "",
            "  Example:",
            f"    ./run.py --repo {repos[0].label} start …",
        ])

    path = pick_repo_interactive(repos)
    # Find label for message
    label = next((r.label for r in repos if r.path == path), path.name)
    os.environ["PIPELINE_REPO"] = str(path)
    sys.stderr.write(f"using repo: {label}  ({path})\n")
    sys.stderr.flush()
    return path


def early_repo_flag(argv: list[str] | None = None) -> str | None:
    """Pull ``--repo`` / ``--repo=`` from argv before full argparse."""
    args = argv if argv is not None else sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--repo":
            if i + 1 < len(args) and not args[i + 1].startswith("-"):
                return args[i + 1]
            return ""
        if a.startswith("--repo="):
            return a.split("=", 1)[1]
    return None
