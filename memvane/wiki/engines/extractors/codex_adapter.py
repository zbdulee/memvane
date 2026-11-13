#!/usr/bin/env python3
"""Codex headless extractor adapter. payload(stdin) -> {"candidates":[...]}(stdout)."""

import sys
from memvane.wiki.engines.extractors import lib

def build_command(b, p, effort=None):
    args = [b, "exec", "-s", "read-only", "--skip-git-repo-check",
            "--ephemeral", "--ignore-user-config", "--ignore-rules"]
    if effort:
        args += ["-c", f"model_reasoning_effort={effort}"]
    return args + [p]


def main():
    return lib.run_adapter(
        "codex",
        "QMD_EXTRACTOR_CODEX_BIN",
        build_command,
        engine="codex",
        supports_effort=True,
    )


if __name__ == "__main__":
    sys.exit(main())
