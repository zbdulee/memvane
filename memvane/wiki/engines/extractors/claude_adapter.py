#!/usr/bin/env python3
"""Claude headless extractor adapter. payload(stdin) -> {"candidates":[...]}(stdout)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.resolve()))
import lib

def build_command(b, p, effort=None):
    args = [b, "-p", "--safe-mode", "--tools", "", "--permission-mode", "plan",
            "--output-format", "text", "--no-session-persistence"]
    if effort:
        args += ["--effort", effort]
    return args + [p]


sys.exit(lib.run_adapter(
    "claude",
    "QMD_EXTRACTOR_CLAUDE_BIN",
    build_command,
    engine="claude",
    supports_effort=True,
))
