#!/usr/bin/env python3
"""Edit-hook payload helpers: edited paths and added text of a host edit event."""
import sys
import re
from pathlib import Path

# Add current directory to path to import core sibling modules
sys.path.insert(0, str(Path(__file__).parent.resolve()))

MAX_TEXT_CHARS = 1600

def normalize_path(path_str: str, cwd: str) -> str:
    if not path_str:
        return ""
    p = Path(path_str)
    try:
        if p.is_absolute():
            return p.resolve().relative_to(Path(cwd).resolve()).as_posix()
    except (OSError, ValueError):
        pass
    normalized = path_str.replace("\\", "/")
    if normalized.startswith("./"):
        return normalized[2:]
    return normalized

def is_story_path(path_str: str, cwd: str, config: dict) -> bool:
    normalized = normalize_path(path_str, cwd)
    
    col_paths = config.get("collectionPaths", {})
    if not isinstance(col_paths, dict) or not col_paths:
        return False

    story_dirs = []
    for val in col_paths.values():
        if isinstance(val, str):
            val_clean = val.strip("/")
            if val_clean and val_clean not in story_dirs:
                story_dirs.append(val_clean)
                
    for d in story_dirs:
        if normalized == d or normalized.startswith(d + "/"):
            return True
    return False

def paths_from_patch(patch: str) -> list[str]:
    paths = []
    for line in patch.splitlines():
        match = re.match(r"\*\*\* (?:Update|Add|Delete) File: (.+)", line)
        if match:
            paths.append(match.group(1).strip())
        moved = re.match(r"\*\*\* Move to: (.+)", line)
        if moved:
            paths.append(moved.group(1).strip())
    return paths

def added_text_from_patch(patch: str) -> str:
    lines = []
    for line in patch.splitlines():
        if not line.startswith("+"):
            continue
        text = line[1:].strip()
        if text:
            lines.append(text)
    return "\n".join(lines)

def extract_text(payload: dict) -> str:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return ""

    chunks = []
    for key in (
        "content",
        "new_string",
        "CodeContent",
        "ReplacementContent",
        "Content",
        "NewString",
    ):
        value = tool_input.get(key)
        if isinstance(value, str):
            chunks.append(value)

    patch = tool_input.get("patch")
    if not isinstance(patch, str) and payload.get("tool_name") == "apply_patch":
        patch = tool_input.get("command")
    if isinstance(patch, str):
        chunks.append(added_text_from_patch(patch))

    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                value = edit.get("new_string") or edit.get("content")
                if isinstance(value, str):
                    chunks.append(value)

    replacements = tool_input.get("Replacements") or tool_input.get("ReplacementChunks")
    if isinstance(replacements, list):
        for edit in replacements:
            if isinstance(edit, dict):
                value = edit.get("ReplacementContent") or edit.get("CodeContent") or edit.get("Content")
                if isinstance(value, str):
                    chunks.append(value)

    text = "\n".join(chunk for chunk in chunks if chunk.strip())
    return text[:MAX_TEXT_CHARS]

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

def story_paths_touched(payload: dict, cwd: str, config: dict) -> bool:
    return any(is_story_path(p, cwd, config) for p in edited_paths(payload))
