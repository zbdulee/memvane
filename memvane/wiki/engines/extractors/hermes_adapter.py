#!/usr/bin/env python3
"""Hermes headless extractor adapter. payload(stdin) -> {"candidates":[...]}(stdout)."""

import sys
from memvane.wiki.engines.extractors import lib

# NOTE: hermes has no documented flag to disable session/checkpoint persistence
# (unlike claude --no-session-persistence / codex --ephemeral). `-z --safe-mode`
# is the most isolated one-shot mode available; a session record may still land in
# ~/.hermes. Add a suppression flag here if/when hermes exposes one.
def build_command(b, p, effort=None):
    return [b, "-z", p, "--safe-mode", "--ignore-user-config", "--ignore-rules", "-t", ""]


sys.exit(lib.run_adapter(
    "hermes",
    "QMD_EXTRACTOR_HERMES_BIN",
    build_command,
    engine="hermes",
    supports_effort=False,
))
