"""Read-only hook boundary for the one generated-story pilot."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import config
import posttool
from context_learning.synthetic_scope import invocation_in_generated_story


EDIT_ACTIONS = {"gate", "posttool", "index", "compile"}


def allowed(action, payload, fallback_cwd):
    if not isinstance(payload, dict):
        return False
    raw_cwd = payload.get("cwd")
    # Only the host's cwd or the adapter-supplied fallback may select config.
    # Ignore extra event fields; a tool payload must not select another project.
    effective = raw_cwd or fallback_cwd
    if not isinstance(effective, str) or not Path(effective).is_absolute():
        return False
    found = config.find_project_config(effective)
    targets = posttool.edited_paths(payload) if action in EDIT_ACTIONS else None
    return invocation_in_generated_story(found, raw_cwd, targets)


def main():
    if len(sys.argv) != 4:
        return 1
    action, source, fallback = sys.argv[1:]
    try:
        if source == "-":
            raw = sys.stdin.read(65537)
        else:
            path = Path(source)
            if path.is_symlink() or path.stat().st_size > 65536:
                return 1
            raw = path.read_text(encoding="utf8")
        payload = json.loads(raw)
        return 0 if allowed(action, payload, fallback) else 1
    except (OSError, ValueError, TypeError, UnicodeError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
