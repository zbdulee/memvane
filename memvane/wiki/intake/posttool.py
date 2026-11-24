#!/usr/bin/env python3
"""Edit-hook payload helpers: edited paths and added text of a host edit event."""
import re
from pathlib import Path

# Add current directory to path to import core sibling modules




def paths_from_patch(patch: str) -> list[str]:
    paths = []
    for line in patch.split('\n'):
        line = line.removesuffix('\r')
        match = re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+)", line)
        if match:
            paths.append(match.group(1).strip(' \t'))
        moved = re.match(r"\*\*\* Move to: (.+)", line)
        if moved:
            paths.append(moved.group(1).strip(' \t'))
    return paths



def edited_paths(payload: dict) -> list[str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    paths = []
    for key in ("file_path", "path", "notebook_path", "TargetFile", "FilePath", "Path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            paths.append(value)
    patch = tool_input.get("patch")
    # Codex PostToolUse reports its canonical `apply_patch` payload in
    # `tool_input.command`, while Claude-compatible callers use `patch`.
    # Do not reinterpret arbitrary command tools as patches.
    if not isinstance(patch, str) and payload.get("tool_name") == "apply_patch":
        patch = tool_input.get("command")
    if isinstance(patch, str):
        paths.extend(paths_from_patch(patch))
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("file_path") or edit.get("path")
                if isinstance(value, str):
                    paths.append(value)
    replacements = tool_input.get("Replacements") or tool_input.get("ReplacementChunks")
    if isinstance(replacements, list):
        for edit in replacements:
            if isinstance(edit, dict):
                value = edit.get("TargetFile") or edit.get("FilePath") or edit.get("Path")
                if isinstance(value, str):
                    paths.append(value)
    return paths
