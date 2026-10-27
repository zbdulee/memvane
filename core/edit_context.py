"""Bind a real edit tool's before/after bytes for the approved synthetic story.

The PreToolUse snapshot is needed for deletion. It is deliberately local and
content-free outside the owner-only receipt; no shell command is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time

import config
import cooldown as qmd_cooldown
import posttool
from context_learning.safe_source import snapshot_beneath
from context_learning.dataset import read_private_json
from context_learning.synthetic_experiment import shared_for, story_for
from context_learning.synthetic_scope import NOVEL_ROOT

MAX_EVENT_BYTES = 65536
RECEIPT_TTL_SECONDS = 600
MAX_PENDING_RECEIPTS = 128

_JANITOR = '''import os, stat, sys, time
path, device, inode = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
sys.path.insert(0, sys.argv[4])
from cooldown import window_elapsed
time.sleep(600)
try:
    parent = os.lstat(os.path.dirname(path))
    entry = os.lstat(path)
    if (stat.S_ISDIR(parent.st_mode) and parent.st_uid == os.getuid()
            and not parent.st_mode & 0o077 and stat.S_ISREG(entry.st_mode)
            and entry.st_uid == os.getuid() and not entry.st_mode & 0o077
            and (entry.st_dev, entry.st_ino) == (device, inode)
            and window_elapsed(entry.st_mtime, time.time(), 600)):
        os.unlink(path)
except OSError:
    pass
'''


def _approved(root, story):
    """Separate owner-held real-edit enrollment; config opt-in is insufficient."""
    private = shared_for(root)
    approval = Path.home() / '.config/laya/real-edit-approval/current.json'
    if approval.is_symlink() or not approval.is_file():
        raise ValueError('edit_approval_missing')
    owner = approval.parent.stat()
    if (any(component.is_symlink() for component in
            (approval.parent, *approval.parent.parents)) or owner.st_uid != os.getuid()
            or owner.st_mode & 0o077):
        raise ValueError('edit_approval_unsafe')
    value = read_private_json(approval)
    from context_learning.synthetic_scope import APPROVAL_FILE
    parent = Path(APPROVAL_FILE)
    if parent.is_symlink() or not parent.is_file():
        raise ValueError('edit_parent_approval_missing')
    info = parent.stat()
    if info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('edit_parent_approval_unsafe')
    expected = {'schema': 'qmd-real-edit-approval-v1', 'enabled': True,
                'projectRoot': str(root), 'storyRoot': str(story),
                'sharedRoot': str(private), 'allowLocalCapture': True,
                'allowExternalGold': True,
                'parentApprovalSha256': hashlib.sha256(parent.read_bytes()).hexdigest()}
    if value != expected:
        raise ValueError('edit_approval_changed')
    return value


def _identity(payload):
    values = _host_ids(payload)
    if values is None:
        raise ValueError("edit_hook_identity_missing")
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _host_ids(payload):
    """Codex has a turn ID; Claude tool hooks have session and tool IDs."""
    session, turn, tool = (payload.get(key) for key in
                           ('session_id', 'turn_id', 'tool_use_id'))
    if (not isinstance(session, str) or not 1 <= len(session) <= 160
            or not isinstance(tool, str) or not 1 <= len(tool) <= 160
            or turn is not None and (not isinstance(turn, str)
                                     or not 1 <= len(turn) <= 160)):
        return None
    # The same sentinel is carried through the schema-v4 ticket. It cannot
    # collide with a real Codex turn because the host's turn field is present.
    return session, turn if turn is not None else 'no-turn', tool


def _scope(payload):
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("edit_hook_cwd_missing")
    found = config.find_project_config(cwd)
    root = Path(found["projectRoot"]).resolve()
    if root != NOVEL_ROOT.resolve():
        raise ValueError("edit_hook_not_pilot")
    options = found["config"]
    shared = options.get("sharedLearning")
    learning = options.get("contextLearning")
    if (options.get("indexing") is not True
            or not isinstance(shared, dict) or shared.get("enabled") is not True
            or not isinstance(learning, dict) or learning.get("capture") is not True):
        raise ValueError("edit_hook_not_enrolled")
    story = story_for(root).resolve(strict=True)
    if not Path(cwd).resolve().is_relative_to(story):
        raise ValueError("edit_hook_outside_story")
    _approved(root, story)
    paths = posttool.edited_paths(payload)
    if not paths or len(paths) > 8:
        raise ValueError("edit_hook_paths_missing")
    relatives = []
    allowed = set()
    for value in options.get("collectionPaths", {}).values():
        if isinstance(value, str) and value.startswith(story.name + "/"):
            allowed.add(Path(value).relative_to(story.name))
    for path in paths:
        target = (Path(cwd) / path).resolve(strict=False)
        try:
            relative = target.relative_to(story)
        except ValueError as exc:
            raise ValueError("edit_hook_outside_story") from exc
        wiki_card = (relative.parts[:2] == ('.auto-context', 'wiki')
                     and len(relative.parts) > 2
                     and all(not part.startswith('.') for part in relative.parts[2:]))
        if (not relative.parts or
                (not wiki_card and any(part.startswith(".") for part in relative.parts))
                or not any(relative == base or base in relative.parents for base in allowed)):
            raise ValueError("edit_hook_path_unapproved")
        if relative.as_posix() not in relatives:
            relatives.append(relative.as_posix())
    return root, story, relatives


def _receipt(root, key):
    folder = shared_for(root) / "edit-preimages"
    folder.mkdir(mode=0o700, exist_ok=True)
    info = folder.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("edit_hook_receipt_dir_unsafe")
    return folder / (key + ".json")


def _prune(folder):
    """Remove only expired, owner-held derivative receipts, never source files."""
    pending = 0
    now = time.time()
    for entry in folder.iterdir():
        if not entry.name.endswith('.json') or len(entry.name) != 69:
            continue
        try:
            info = entry.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                continue
            if qmd_cooldown.window_elapsed(info.st_mtime, now, RECEIPT_TTL_SECONDS):
                entry.unlink()
            else:
                pending += 1
        except FileNotFoundError:
            continue
    return pending


def _schedule_prune(path):
    info = path.stat()
    subprocess.Popen([sys.executable, '-c', _JANITOR, str(path),
                      str(info.st_dev), str(info.st_ino),
                      str(Path(__file__).resolve().parent)], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, start_new_session=True)


def _changed_region(previous, current, *, context_chars=256):
    """Return the exact changed span with small context, not a whole old file.

    A create/delete still uses all available content. Multiple distant edits
    remain one enclosing span; callers must segment it, never silently trim it.
    """
    if previous is None or current is None:
        return (current if current is not None else previous), 0, ('before' if current is None else 'after')
    prefix = 0
    upper = min(len(previous), len(current))
    while prefix < upper and previous[prefix] == current[prefix]:
        prefix += 1
    old_end, new_end = len(previous), len(current)
    while (old_end > prefix and new_end > prefix
           and previous[old_end - 1] == current[new_end - 1]):
        old_end -= 1
        new_end -= 1
    # A pure removal has no replacement text. Use the removed source span so
    # the search can still recover the wiki facts implicated by the edit.
    from_before = new_end == prefix
    source, end = (previous, old_end) if from_before else (current, new_end)
    start = max(0, prefix - context_chars)
    end = min(len(source), end + context_chars)
    return source[start:end], start, ('before' if from_before else 'after')


def before(payload):
    if payload.get("hook_event_name") != "PreToolUse":
        raise ValueError("edit_hook_wrong_event")
    root, story, relatives = _scope(payload)
    path = _receipt(root, _identity(payload))
    if _prune(path.parent) >= MAX_PENDING_RECEIPTS:
        raise ValueError('edit_hook_receipt_capacity')
    entries = []
    for relative in relatives:
        try:
            body = snapshot_beneath(story, relative)
        except FileNotFoundError:
            body = None
        if body is not None and len(body) > MAX_EVENT_BYTES:
            raise ValueError("edit_hook_preimage_oversize")
        entries.append({"path": relative, "before": body.decode("utf8") if body is not None else None})
    data = json.dumps({"schema": 1, "entries": entries}, ensure_ascii=False).encode()
    if len(data) > MAX_EVENT_BYTES * 8:
        raise ValueError("edit_hook_receipt_oversize")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _schedule_prune(path)
    except Exception:
        # Without a crash-independent expiry worker, retaining raw preimages
        # would be unbounded. Remove the newly created derivative instead.
        path.unlink()
        raise ValueError('edit_hook_janitor_unavailable')
    return "stored"


def after(payload):
    if payload.get("hook_event_name") != "PostToolUse":
        raise ValueError("edit_hook_wrong_event")
    root, story, relatives = _scope(payload)
    path = _receipt(root, _identity(payload))
    _prune(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError("edit_hook_preimage_missing")
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o077 or
            qmd_cooldown.window_elapsed(info.st_mtime, time.time(), RECEIPT_TTL_SECONDS)):
        raise ValueError("edit_hook_receipt_unsafe")
    recorded = json.loads(path.read_text(encoding="utf8"))
    if (set(recorded) != {"schema", "entries"} or recorded["schema"] != 1
            or [row["path"] for row in recorded["entries"]] != relatives):
        raise ValueError("edit_hook_preimage_mismatch")
    # The receipt is single use even if a later source snapshot fails.
    path.unlink()
    chunks = []
    for row in recorded["entries"]:
        relative, previous = row["path"], row["before"]
        try:
            current = snapshot_beneath(story, relative).decode("utf8")
        except FileNotFoundError:
            current = None
        if current is not None and len(current.encode('utf8')) > MAX_EVENT_BYTES:
            raise ValueError('edit_hook_postimage_oversize')
        if current == previous:
            continue
        source, source_start, source_side = _changed_region(previous, current)
        if not source or len(source.encode()) > MAX_EVENT_BYTES:
            continue
        operation = "delete" if current is None else "write" if previous is None else "edit"
        chunks.append({'operation': operation, 'path': relative,
                       'text': source, 'source_start': source_start,
                       'before_content': previous, 'after_content': current,
                       'before_sha256': hashlib.sha256(previous.encode()).hexdigest() if previous is not None else None,
                       'after_sha256': hashlib.sha256(current.encode()).hexdigest() if current is not None else None,
                       'document_sha256': hashlib.sha256(
                           (previous if source_side == 'before' else current).encode()).hexdigest(),
                       'source_side': source_side,
                       'source_sha256': hashlib.sha256(source.encode()).hexdigest()})
    if not chunks:
        return None
    text = "\n\n".join(f"{entry['operation']} {entry['path']}:\n{entry['text']}"
                       for entry in chunks)
    if len(text.encode()) > MAX_EVENT_BYTES:
        raise ValueError("edit_hook_context_oversize")
    session, turn, tool = _host_ids(payload) or (None, None, None)
    if session is None:
        raise ValueError('edit_hook_identity_missing')
    event = {"prompt": text, "changes": chunks, "context_origin": "tool_edit",
             "edit_session_id": session, "edit_tool_use_id": tool,
             "edit_turn_id": turn}
    # Hand the completed preimage comparison to the registration path through
    # an owner-only, single-use receipt. A caller-supplied event dict alone is
    # not evidence that before()/after() saw the edit.
    proof = json.dumps({"schema": 1, "kind": "completed_edit", "event": event},
                       ensure_ascii=False, sort_keys=True).encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(proof)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _schedule_prune(path)
    except Exception:
        path.unlink()
        raise ValueError('edit_hook_janitor_unavailable')
    return event


def consume_completed_edit(project, event):
    """Consume the one-use result written by after(), before minting tickets."""
    if not isinstance(event, dict):
        raise ValueError('edit_hook_completed_event_invalid')
    project = Path(project).resolve(strict=True)
    payload = {'session_id': event.get('edit_session_id'),
               'turn_id': event.get('edit_turn_id'),
               'tool_use_id': event.get('edit_tool_use_id')}
    path = _receipt(project, _identity(payload))
    _prune(path.parent)
    if path.is_symlink() or not path.is_file():
        raise ValueError('edit_hook_completed_receipt_missing')
    info = path.stat()
    if (info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o077
            or qmd_cooldown.window_elapsed(info.st_mtime, time.time(), RECEIPT_TTL_SECONDS)):
        raise ValueError('edit_hook_completed_receipt_unsafe')
    recorded = json.loads(path.read_text(encoding='utf8'))
    if (recorded != {'schema': 1, 'kind': 'completed_edit', 'event': event}):
        raise ValueError('edit_hook_completed_receipt_mismatch')
    path.unlink()


def main():
    try:
        payload = json.load(sys.stdin)
        if len(sys.argv) != 2 or sys.argv[1] != "pre":
            return 1
        before(payload)
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError):
        return 0  # The existing gate remains authoritative for edit permission.


if __name__ == "__main__":
    raise SystemExit(main())
