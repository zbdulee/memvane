"""Run a bounded lifecycle child for a selected project."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess


from memvane.wiki.workspace.types import LIFECYCLE_SECONDS


def run_lifecycle(project: Path) -> dict:
    """One bounded lifecycle child for this project; fail-open."""
    from memvane.wiki.lifecycle import wiki_lifecycle
    from memvane.kernel.bundle_paths import entry_argv
    argv = entry_argv("wiki-lifecycle", "--cwd", str(project))
    try:
        proc = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, check=False, timeout=LIFECYCLE_SECONDS,
                              env={**os.environ, "QMD_RUNTIME_DISPATCH": "1"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        wiki_lifecycle.log(f"EXCEPTION: lifecycle child {type(exc).__name__}")
        return {"state": "failed", "reason": type(exc).__name__}
    if proc.returncode:
        wiki_lifecycle.log(f"EXCEPTION: lifecycle child exit {proc.returncode}")
        return {"state": "failed", "reason": "exit_" + str(proc.returncode)}
    return {"state": "ran"}
