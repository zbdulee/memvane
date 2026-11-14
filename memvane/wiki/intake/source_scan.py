"""Bounded source scans for configured collection roots."""

from __future__ import annotations
from memvane.wiki.intake.types import MAX_DEPTH

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from memvane.kernel.errors import IndexContractError, _digest
from memvane.settings.api import is_source_text
from memvane.settings.types import DENIED_SOURCE_SEGMENTS, SOURCE_TEXT_EXTENSIONS
from memvane.settings.types import WIKI_TEXT_EXTENSIONS

STALE = "index_stale"
EXCLUSION_KINDS = frozenset({"path", "hash"})
MAX_ENTRIES = 50_000
MAX_REL_PATH_BYTES = 4096
_READ_CHUNK = 64 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | _CLOEXEC
_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_WIKI_LOG_HEADER = '# Auto-context Wiki Log\n\n'


def generated_wiki_log_text(body: str) -> bool:
    """The exact header earlier releases wrote when they created the root log.

    A partially appended event must not turn this maintenance file into a
    wiki card. The caller must also prove the path is the bound wiki root.
    """
    return isinstance(body, str) and body.startswith(_WIKI_LOG_HEADER)




def generated_wiki_log_file(dfd: int, name: str, expected: os.stat_result) -> bool:
    """Classify a root log no-follow, before it consumes an embedding budget."""
    fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _sig(before) != _sig(expected):
            _raise_stale('wiki log changed before classification')
        matched = os.read(fd, len(_WIKI_LOG_HEADER.encode('utf-8'))) == \
            _WIKI_LOG_HEADER.encode('utf-8')
        if _sig(os.fstat(fd)) != _sig(before):
            _raise_stale('wiki log changed during classification')
        return matched
    finally:
        os.close(fd)


class SourceStale(Exception):
    """Diagnostic cause chained onto IndexContractError('index_stale')."""


def _raise_stale(detail: str, cause: BaseException | None = None) -> NoReturn:
    reason = SourceStale(detail)
    reason.__cause__ = cause
    raise IndexContractError(STALE) from reason


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    eligible: bool
    data: bytes

    @property
    def text(self) -> str:
        return self.data.decode("utf-8")


@dataclass(frozen=True)
class SourceScan:
    root: str
    scope: str
    files: tuple[SourceFile, ...]
    manifest: str
    by_path: Mapping[str, SourceFile]
    # Source-text files skipped because their bytes are not UTF-8.
    undecodable: tuple[str, ...] = ()

    def documents(self) -> list[tuple[str, str, bool]]:
        # Excluded bytes remain retained under the current no-physical-GC policy.
        return [(f.path, f.text, f.eligible) for f in self.files]


@dataclass(frozen=True)
class CollectionSourceScan:
    roots: Mapping[str, str]
    scope: str
    files: tuple[SourceFile, ...]
    manifest: str
    by_path: Mapping[str, SourceFile]
    undecodable: tuple[str, ...] = ()

    def documents(self) -> list[tuple[str, str, bool]]:
        return [(f.path, f.text, f.eligible) for f in self.files]


@dataclass
class _Budget:
    max_files: int
    max_bytes: int
    files: int = 0
    bytes: int = 0
    entries: int = 0
    undecodable: list = None

    def __post_init__(self) -> None:
        if self.undecodable is None:
            self.undecodable = []


def _positive_int(name: str, value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive int")
    return value


def _check_root(root: Any) -> str:
    if not isinstance(root, str) or not root:
        raise TypeError("root must be a non-empty str")
    if "\x00" in root or not os.path.isabs(root):
        _raise_stale("root must be an absolute path")
    if os.path.realpath(root) != root:
        _raise_stale("root must be canonical (no symlinks, '.', '..' or trailing '/')")
    return root


def _open_root_fd(root: str) -> int:
    """Traverse every ancestor without following a substituted symlink."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        for component in (part for part in root.split("/") if part):
            next_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        named = os.stat(root, follow_symlinks=False)
        if _node(os.fstat(fd)) != _node(named):
            _raise_stale("source root changed while opening")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _require_platform() -> None:
    if os.stat not in os.supports_dir_fd or os.open not in os.supports_dir_fd:
        _raise_stale("platform lacks dir_fd support")
    if os.scandir not in os.supports_fd:
        _raise_stale("platform lacks fd scandir support")


def load_exclusions(index: Any, scope: str) -> tuple[frozenset[str], frozenset[str]]:
    # Assumed accessor over the `exclusions` table: rows of (scope, kind, value).
    try:
        rows = list(index.list_exclusions(scope))
    except IndexContractError:
        raise
    except Exception as exc:
        _raise_stale("exclusions unavailable", exc)
    paths: set[str] = set()
    hashes: set[str] = set()
    for row in rows:
        try:
            row_scope, kind, value = row
        except (TypeError, ValueError) as exc:
            _raise_stale("malformed exclusion row", exc)
        if row_scope != scope or kind not in EXCLUSION_KINDS or not isinstance(value, str):
            _raise_stale("malformed exclusion row")
        (paths if kind == "path" else hashes).add(value)
    return frozenset(paths), frozenset(hashes)


def _safe_rel(prefix: str, name: str) -> str:
    if name in (".", "..") or "/" in name or "\\" in name:
        _raise_stale("unsafe name")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        _raise_stale("control character in name")
    rel = prefix + name
    try:
        encoded = rel.encode("utf-8")
    except UnicodeEncodeError as exc:
        _raise_stale("non-UTF-8 name", exc)
    if len(encoded) > MAX_REL_PATH_BYTES:
        _raise_stale("path too long")
    return rel


def _node(st: os.stat_result) -> tuple[int, int]:
    return st.st_dev, st.st_ino


def _sig(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _read_file(dfd: int, name: str, rel: str, st: os.stat_result, budget: _Budget,
               strict_utf8: bool = True):
    budget.files += 1
    if budget.files > budget.max_files:
        _raise_stale("too many files")
    limit = budget.max_bytes - budget.bytes
    if st.st_size > limit:
        _raise_stale(f"oversize: {rel}")
    fd = os.open(name, _FILE_FLAGS, dir_fd=dfd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or _sig(before) != _sig(st):
            _raise_stale(f"changed before read: {rel}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(_READ_CHUNK, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                _raise_stale(f"oversize: {rel}")
            chunks.append(chunk)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if _sig(after) != _sig(before) or len(data) != before.st_size:
        _raise_stale(f"changed during read: {rel}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        if strict_utf8:
            _raise_stale(f"invalid UTF-8: {rel}", exc)
        # Not a text source: it stays out of the snapshot and is reported.
        budget.undecodable.append(rel)
        return None
    budget.bytes += len(data)
    return rel, hashlib.sha256(data).hexdigest(), data


def _walk(dfd: int, dst: os.stat_result, prefix: str, depth: int,
          root_dev: int, budget: _Budget, out: list,
          wiki_only_root: bool = False,
          extensions: tuple[str, ...] = SOURCE_TEXT_EXTENSIONS,
          strict_utf8: bool = False) -> None:
    if depth > MAX_DEPTH:
        _raise_stale("too deep")
    budget.entries += 1
    if budget.entries > MAX_ENTRIES:
        _raise_stale("too many entries")
    with os.scandir(dfd) as it:
        entries = sorted(it, key=lambda entry: entry.name)
    for entry in entries:
        name = entry.name
        if name.startswith(".") or name in DENIED_SOURCE_SEGMENTS:
            continue
        text = is_source_text(name, extensions)
        # d_type pre-filter: a non-text entry that is not a real directory is
        # never stat'ed, so its races and failures cannot fail the scan.
        if not text and not entry.is_dir(follow_symlinks=False):
            continue
        rel = _safe_rel(prefix, name)
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            if text:
                _raise_stale(f"symlink: {rel}")
            continue
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != root_dev:
                continue
            cfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            try:
                cst = os.fstat(cfd)
                if _node(cst) != _node(st):
                    _raise_stale(f"directory replaced: {rel}")
                _walk(cfd, cst, rel + "/", depth + 1, root_dev, budget, out,
                      extensions=extensions, strict_utf8=strict_utf8)
            finally:
                os.close(cfd)
        elif text:
            if not stat.S_ISREG(st.st_mode):
                _raise_stale(f"not a regular file: {rel}")
            if st.st_dev != root_dev:
                _raise_stale(f"crosses device: {rel}")
            budget.entries += 1
            if budget.entries > MAX_ENTRIES:
                _raise_stale("too many entries")
            if wiki_only_root and name == 'log.md':
                if generated_wiki_log_file(dfd, name, st):
                    continue
            item = _read_file(dfd, name, rel, st, budget, strict_utf8)
            if item is not None:
                out.append(item)
    after = os.fstat(dfd)
    if _sig(after) != _sig(dst):
        _raise_stale(f"directory changed during scan: {prefix or '.'}")


def scan(root: str, scope: str, index: Any,
         max_files: int = 200, max_bytes: int = 5_000_000) -> SourceScan:
    root = _check_root(root)
    if not isinstance(scope, str) or not scope:
        raise TypeError("scope must be a non-empty str")
    budget = _Budget(_positive_int("max_files", max_files), _positive_int("max_bytes", max_bytes))
    _require_platform()
    excluded_paths, excluded_hashes = load_exclusions(index, scope)
    raw: list[tuple[str, str, bytes]] = []
    try:
        root_fd = _open_root_fd(root)
    except OSError as exc:
        _raise_stale("root unavailable", exc)
    try:
        root_st = os.fstat(root_fd)
        _walk(root_fd, root_st, "", 0, root_st.st_dev, budget, raw)
    except OSError as exc:
        _raise_stale("source unreadable", exc)
    finally:
        os.close(root_fd)
    raw.sort(key=lambda item: item[0])
    files = tuple(
        SourceFile(path, digest, path not in excluded_paths and digest not in excluded_hashes, data)
        for path, digest, data in raw
    )
    return SourceScan(
        root=root,
        scope=scope,
        files=files,
        manifest=_digest(scope, [(f.path, f.sha256, int(f.eligible)) for f in files]),
        by_path=MappingProxyType({f.path: f for f in files}),
        undecodable=tuple(sorted(budget.undecodable)),
    )


def scan_collections(roots: Mapping[str, str], scope: str, index: Any,
                     max_files: int = 200,
                     max_bytes: int = 5_000_000, *,
                     wiki_only: bool = False) -> CollectionSourceScan:
    """Scan explicit disjoint roots into one namespaced A-int generation.

    The route/cutover proof binds the physical roots. A-int's source manifest
    binds their sorted ``collection/relative`` text members under one scope.
    No project-root traversal is used: a hidden wiki root is entered only when
    it was explicitly approved as a collection root. ``index`` None is a
    store without an exclusion table (a schema3 selection's raw discovery):
    every scanned file is eligible.
    """
    if (not isinstance(roots, Mapping) or not 1 <= len(roots) <= 16
            or not isinstance(scope, str) or not scope):
        raise ValueError("invalid collection roots")
    if wiki_only and len(roots) != 1:
        raise ValueError('wiki-only scan requires one root')
    if any(not isinstance(name, str) or not _COLLECTION.fullmatch(name)
           for name in roots):
        raise ValueError("invalid collection name")
    ordered: dict[str, str] = {}
    for name, root in sorted(roots.items()):
        ordered[name] = _check_root(root)
    root_values = tuple(ordered.values())
    if len(set(root_values)) != len(root_values) or any(
            left.startswith(right + "/") or right.startswith(left + "/")
            for i, left in enumerate(root_values) for right in root_values[i + 1:]):
        _raise_stale("collection roots overlap")
    budget = _Budget(_positive_int("max_files", max_files),
                     _positive_int("max_bytes", max_bytes))
    _require_platform()
    excluded_paths, excluded_hashes = ((frozenset(), frozenset()) if index is None
                                       else load_exclusions(index, scope))
    raw: list[tuple[str, str, bytes]] = []
    for name, root in ordered.items():
        try:
            root_fd = _open_root_fd(root)
        except OSError as exc:
            _raise_stale("collection root unavailable", exc)
        try:
            root_st = os.fstat(root_fd)
            _walk(root_fd, root_st, name + "/", 0, root_st.st_dev,
                  budget, raw, wiki_only_root=wiki_only,
                  extensions=WIKI_TEXT_EXTENSIONS if wiki_only else SOURCE_TEXT_EXTENSIONS,
                  strict_utf8=wiki_only)
        except OSError as exc:
            _raise_stale("collection source unreadable", exc)
        finally:
            os.close(root_fd)
    raw.sort(key=lambda item: item[0])
    files = tuple(SourceFile(path, digest,
                             path not in excluded_paths and digest not in excluded_hashes,
                             data) for path, digest, data in raw)
    return CollectionSourceScan(
        roots=MappingProxyType(ordered), scope=scope, files=files,
        manifest=_digest(scope, [(f.path, f.sha256, int(f.eligible)) for f in files]),
        by_path=MappingProxyType({f.path: f for f in files}),
        undecodable=tuple(sorted(budget.undecodable)),
    )



_MAX_COLLECTIONS = 16


def _collection_roots(roots):
    """Return a frozen sorted copy of valid disjoint roots, else None."""
    try:
        items = sorted(roots.items())
    except (TypeError, ValueError):
        return None
    if not 1 <= len(items) <= _MAX_COLLECTIONS:
        return None
    paths = []
    for name, root in items:
        if (not isinstance(name, str) or _COLLECTION.fullmatch(name) is None
                or not isinstance(root, str)):
            return None
        try:
            path = Path(root)
            if (not path.is_absolute() or not path.is_dir()
                    or str(path.resolve(strict=True)) != root):
                return None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None
        paths.append(path)
    # Path ancestry, not a string prefix: '/' must overlap every other root.
    if any(left == right or left in right.parents or right in left.parents
           for i, left in enumerate(paths) for right in paths[i + 1:]):
        return None
    return MappingProxyType(dict(items))


_SCHEMA3_ROOT_META = frozenset(('SCHEMA.md', 'index.md', 'log.md'))


def schema3_wiki_cards(root):
    """Sorted card paths under the bound wiki root: regular ``.md`` files only.

    Dot segments, root meta pages and links are not cards; a file the store
    would refuse to admit (not owned, linked, too large) is counted, not read.
    """
    from memvane.store import types as store_types
    cards, skipped = [], 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if not name.startswith('.'))
        relative = os.path.relpath(base, root)
        for name in sorted(files):
            if name.startswith('.') or not name.endswith('.md'):
                continue
            path = name if relative == '.' else relative.replace(os.sep, '/') + '/' + name
            if path in _SCHEMA3_ROOT_META:
                continue
            try:
                info = os.lstat(os.path.join(base, name))
            except FileNotFoundError:
                continue
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_nlink != 1 or info.st_size > store_types.MAX_BODY):
                skipped += 1
                continue
            cards.append(path)
            if len(cards) > store_types.MAX_MEMBERS:
                raise IndexContractError('schema3_wiki_too_many_cards')
    return sorted(cards), skipped


# Raw source observation of one view (design D2): the same bounded raw scan
# v6 discovery runs (laya_wiki_discovery._raw_snapshot), recorded as observed
# source fingerprints of this view's worktree, branch and HEAD. Only names,
# hashes and lengths are kept.
def schema3_sources(decision, handle, deadline, *, raw_scan=None):
    """``{project-relative path: (sha256, byte_length, tracked_state)}`` of the raw roots.

    A completed scan also leaves ``raw_scan['snapshot']`` (``{collection/relative:
    sha256}``, the discovery snapshot shape); a scan over its bounds leaves
    none, so discovery scans (and blocks) on its own.
    """
    from memvane.kernel import paths as git_branch_safety
    from memvane.wiki.intake.types import RAW_SCAN_MAX_BYTES, RAW_SCAN_MAX_FILES
    project = Path(decision['projectRoot'])
    roots = {item['name']: item['sourceRoot'] for item in decision['collections'] if item.get('role') == 'raw'}
    if not roots:
        return {}
    try:
        scan = scan_collections(roots, decision['scopeId'], None, max_files=RAW_SCAN_MAX_FILES,
                                max_bytes=RAW_SCAN_MAX_BYTES)
    except (IndexContractError, ValueError, OSError):
        # Over the scan bounds or unreadable: the generation names no
        # sources (as before D2) instead of holding the wiki publish.
        return {}
    if raw_scan is not None:
        raw_scan['snapshot'] = {item.path: item.sha256 for item in scan.files if item.eligible}
    observed = {}
    for item in scan.files:
        name, rel = item.path.split('/', 1)
        try:
            path = (Path(roots[name]) / rel).relative_to(project).as_posix()
        except ValueError:
            continue  # a raw root outside the project names no project-relative source
        observed[path] = [item.sha256, len(item.data), 'unknown']
    if handle.branch_key != 'non-git' and observed:
        try:
            # Literal pathspecs (git_branch_safety); output is relative to the project.
            specs = sorted({Path(root).relative_to(project).as_posix() for root in roots.values()
                            if Path(root) == project or project in Path(root).parents})
            listed = git_branch_safety.run_git(['ls-files', '-z', '--', *specs], project,
                                               deadline=deadline)
            tracked = set(listed.stdout.decode('utf-8', 'surrogateescape').split('\0'))
            for path, value in observed.items():
                value[2] = 'tracked' if path in tracked else 'untracked'
        except (git_branch_safety.GitUnavailable, OSError, ValueError):
            pass  # unknown tracked state: observed, never deletion authority
    return {path: tuple(value) for path, value in observed.items()}
