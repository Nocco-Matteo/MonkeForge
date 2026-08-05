#!/usr/bin/env python3
"""scripts/wt.py — thin CLI over ``pipeline_graph.worktree_runtime``.

Operators normally use ``./run.py start|resume|land``. This workshop CLI keeps
the same ensure/run/overlap/land/… commands for debugging and tests.

Re-exports the runtime API so ``tests/test_wt.py`` (importlib of this file)
keeps working unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

_MF_ROOT = Path(__file__).resolve().parents[1]
if str(_MF_ROOT) not in sys.path:
    sys.path.insert(0, str(_MF_ROOT))

from pipeline_graph.worktree_runtime import (  # noqa: E402
    DEFAULT_BRANCH_PREFIX,
    ISOLATED_ENV,
    MF_ROOT,
    WorktreeError,
    _E2E_URL_TEMPLATE,
    _SUBCOMMANDS,
    _WT_NAME_RE,
    _build_parser,
    _check_docs_dir_ban,
    _check_yaml_docs_dir_absent,
    _copy_brief,
    _fail,
    _git,
    _load_yaml,
    _yaml_path,
    already_isolated,
    assert_child_env,
    branch_for,
    build_child_env,
    cmd_ensure,
    cmd_land,
    cmd_list,
    cmd_overlap,
    cmd_refresh_brief,
    cmd_remove,
    cmd_run,
    cmd_sync,
    docs_dir_for,
    format_isolation_banner,
    isolation_child_env,
    live_feature_worktrees,
    live_worktrees,
    main,
    pool_port,
    prepare_task_isolation,
    reexec_isolated,
    resolve_branch_prefix,
    resolve_target_repo,
    stable_hash,
    validate_id,
    wt_path_for,
)
import subprocess  # noqa: E402  — re-export for tests that patch wt.subprocess

if __name__ == "__main__":
    sys.exit(main())
