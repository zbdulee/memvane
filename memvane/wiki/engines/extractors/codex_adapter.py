#!/usr/bin/env python3
"""Codex headless extractor adapter. payload(stdin) -> {"candidates":[...]}(stdout)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import lib

def build_command(b, p, effort=None):
    args = [b, "exec", "-s", "read-only", "--skip-git-repo-check",
            "--ephemeral", "--ignore-user-config", "--ignore-rules"]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args + [p]


sys.exit(lib.run_adapter(
    "codex",
    "QMD_EXTRACTOR_CODEX_BIN",
    build_command,
    engine="codex",
    supports_effort=True,
))
