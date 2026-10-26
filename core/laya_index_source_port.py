"""Source-only product port for LayaIndexA.

The port binds an index generation to the exact bytes of an explicitly
approved source root. Every operation does a full scan of that root and then
calls LayaIndexA's sealed API.

Source contract
  * Only non-hidden ``*.md`` regular files are enumerated. Hidden entries
    (leading ``.``) and hidden directories are not walked. Other non-hidden
    non-symlink entries are ignored.
  * Any of the following raises ``IndexContractError('index_stale')`` and
    nothing is skipped silently: a non-hidden symlink (of any name), an unsafe
    or non-UTF-8 name, a device crossing, a ``.md`` name that is not a regular
    file, an unreadable entry, exceeding the file/byte/depth/entry bounds, a
    file or directory that changes between stats, and invalid UTF-8 content.
  * The manifest digests sorted ``(path, sha256, eligible)`` for every
    enumerated file. Excluded files (path or hash exclusions) stay in the
    manifest with ``eligible=False``. Bytes remain stored until a physical-deletion policy exists, but cannot be served.

Guarantees and limitations
  * The port proves scan-time state only. Results reflect the bytes read
    during the final scan. Source changes after that scan (TOCTOU) are not
    detected, and the returned bodies may be outdated by the time the caller
    uses them.
  * ``search_verified`` re-reads the view seal after the search. A revocation
    committed after that final check, or while the caller holds the results,
    is not detected. Exclusions are read once, at scan start.
  * A hard link inside the root that points to content outside it is
    accepted. Scanning keeps whole files in memory, up to ``max_bytes``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, NoReturn

from laya_index_a import IndexContractError, _digest

STALE = "index_stale"
EXCLUSION_KINDS = frozenset({"path", "hash"})
MAX_DEPTH = 32
MAX_ENTRIES = 50_000
MAX_REL_PATH_BYTES = 4096
_READ_CHUNK = 64 * 1024
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | _CLOEXEC
_COLLECTION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_WIKI_LOG_HEADER = '# Auto-context Wiki Log\n\n'


def generated_wiki_log_text(body: str) -> bool:
    """The exact header emitted when the compiler creates the root log.

    A partially appended event must not turn this maintenance file into a
    wiki card. The caller must also prove the path is the bound wiki root.
    """
    return isinstance(body, str) and body.startswith(_WIKI_LOG_HEADER)


def auxiliary_wiki_row(path: str, body: str, wiki_roots) -> bool:
    """Filter only root metadata; a nested log.md is an ordinary wiki card."""
    if len(wiki_roots) != 1:
        return False
    name = next(iter(wiki_roots))
    return (path == name + '/index.md' or
            (path == name + '/log.md' and generated_wiki_log_text(body)))


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

    def documents(self) -> list[tuple[str, str, bool]]:
        return [(f.path, f.text, f.eligible) for f in self.files]


@dataclass
class _Budget:
    max_files: int
    max_bytes: int
    files: int = 0
    bytes: int = 0
    entries: int = 0


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
    if os.listdir not in os.supports_fd:
        _raise_stale("platform lacks fd listdir support")


def _load_exclusions(index: Any, scope: str) -> tuple[frozenset[str], frozenset[str]]:
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


def _read_file(dfd: int, name: str, rel: str, st: os.stat_result, budget: _Budget):
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
        _raise_stale(f"invalid UTF-8: {rel}", exc)
    budget.bytes += len(data)
    return rel, hashlib.sha256(data).hexdigest(), data


def _walk(dfd: int, dst: os.stat_result, prefix: str, depth: int,
          root_dev: int, budget: _Budget, out: list,
          wiki_only_root: bool = False) -> None:
    if depth > MAX_DEPTH:
        _raise_stale("too deep")
    names = sorted(os.listdir(dfd))
    budget.entries += len(names)
    if budget.entries > MAX_ENTRIES:
        _raise_stale("too many entries")
    for name in names:
        if name.startswith("."):
            continue
        rel = _safe_rel(prefix, name)
        st = os.stat(name, dir_fd=dfd, follow_symlinks=False)
        if stat.S_ISLNK(st.st_mode):
            _raise_stale(f"symlink: {rel}")
        if st.st_dev != root_dev:
            _raise_stale(f"crosses device: {rel}")
        if stat.S_ISDIR(st.st_mode):
            cfd = os.open(name, _DIR_FLAGS, dir_fd=dfd)
            try:
                cst = os.fstat(cfd)
                if _node(cst) != _node(st):
                    _raise_stale(f"directory replaced: {rel}")
                _walk(cfd, cst, rel + "/", depth + 1, root_dev, budget, out)
            finally:
                os.close(cfd)
        elif name.endswith(".md"):
            if not stat.S_ISREG(st.st_mode):
                _raise_stale(f"not a regular file: {rel}")
            if wiki_only_root and name == 'log.md':
                if generated_wiki_log_file(dfd, name, st):
                    continue
            out.append(_read_file(dfd, name, rel, st, budget))
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
    excluded_paths, excluded_hashes = _load_exclusions(index, scope)
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
    )


def scan_collections(roots: Mapping[str, str], scope: str, index: Any,
                     max_files: int = 200,
                     max_bytes: int = 5_000_000, *,
                     wiki_only: bool = False) -> CollectionSourceScan:
    """Scan explicit disjoint roots into one namespaced A-int generation.

    The route/cutover proof binds the physical roots. A-int's source manifest
    binds their sorted ``collection/relative.md`` members under one scope.
    No project-root traversal is used: a hidden wiki root is entered only when
    it was explicitly approved as a collection root.
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
    excluded_paths, excluded_hashes = _load_exclusions(index, scope)
    raw: list[tuple[str, str, bytes]] = []
    for name, root in ordered.items():
        try:
            root_fd = _open_root_fd(root)
        except OSError as exc:
            _raise_stale("collection root unavailable", exc)
        try:
            root_st = os.fstat(root_fd)
            _walk(root_fd, root_st, name + "/", 0, root_st.st_dev,
                  budget, raw, wiki_only_root=wiki_only)
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
    )


def stage_collections_from_source(index: Any, roots: Mapping[str, str],
                                  scope: str, gen: Any, *, base: Any,
                                  claim_digest: Any, claim_epoch: Any,
                                  max_files: int = 200,
                                  max_bytes: int = 5_000_000) -> Any:
    source = scan_collections(roots, scope, index, max_files, max_bytes)
    return index.stage(gen, scope, source.documents(), base=base,
                       source_manifest=source.manifest,
                       claim_digest=claim_digest, claim_epoch=claim_epoch)


def publish_collections_from_source(index: Any, roots: Mapping[str, str],
                                    scope: str, expected: Any, gen: Any,
                                    claim: Any, *, claim_digest: Any,
                                    claim_epoch: Any, require_vector: bool = False,
                                    max_files: int = 200,
                                    max_bytes: int = 5_000_000) -> Any:
    source = scan_collections(roots, scope, index, max_files, max_bytes)
    return index.publish(expected, gen, claim,
                         source_manifest=source.manifest,
                         claim_digest=claim_digest, claim_epoch=claim_epoch,
                         require_vector=require_vector)


def stage_from_source(index: Any, root: str, scope: str, gen: Any, *, base: Any,
                      claim_digest: Any, claim_epoch: Any,
                      max_files: int = 200, max_bytes: int = 5_000_000) -> Any:
    source = scan(root, scope, index, max_files, max_bytes)
    return index.stage(
        gen, scope, source.documents(),
        base=base, source_manifest=source.manifest,
        claim_digest=claim_digest, claim_epoch=claim_epoch,
    )


def publish_from_source(index: Any, root: str, scope: str, expected: Any, gen: Any, claim: Any, *,
                        claim_digest: Any, claim_epoch: Any,
                        require_vector: bool = False,
                        max_files: int = 200, max_bytes: int = 5_000_000) -> Any:
    # The index checks this fresh manifest against the staged seal and does the CAS.
    source = scan(root, scope, index, max_files, max_bytes)
    return index.publish(
        expected, gen, claim,
        source_manifest=source.manifest,
        claim_digest=claim_digest, claim_epoch=claim_epoch,
        require_vector=require_vector,
    )


def _seal(index: Any, token: Any) -> tuple[str, Any, str, Any]:
    seal = index.view_seal(token)
    try:
        scope = seal["scope"]
        manifest = seal["source_manifest"]
        if (not isinstance(scope, str) or not scope or not isinstance(manifest, str)
                or len(manifest) != 64):
            raise ValueError("malformed view seal")
        return scope, seal["gen"], manifest, seal["rev_epoch"]
    except (KeyError, TypeError, ValueError) as exc:
        _raise_stale("malformed view seal", exc)


def _verify_row(row: Mapping[str, Any], scope: str, gen: Any, source: SourceScan) -> dict[str, Any]:
    f = source.by_path.get(row["path"])
    if (row["scope"] != scope or row["generation"] != gen or f is None
            or not f.eligible or row["hash"] != f.sha256):
        _raise_stale("search row does not match source")
    return {
        "scope": scope,
        "generation": gen,
        "path": f.path,
        "hash": f.sha256,
        "body": f.text,  # decoded from the same buffer that was hashed
        "score": row["score"],
    }


def search_verified(index: Any, root: str, token: Any, fts_query: str, limit: int, *,
                    max_files: int = 200, max_bytes: int = 5_000_000) -> list[dict[str, Any]]:
    limit = _positive_int("limit", limit)
    seal = _seal(index, token)
    scope, gen, sealed_manifest, _ = seal
    source = scan(root, scope, index, max_files, max_bytes)
    if sealed_manifest != source.manifest:
        _raise_stale("source manifest differs from view seal")
    rows = index.search(token, fts_query, limit)
    try:
        rows = list(rows)
        if len(rows) > limit:
            raise ValueError("too many rows")
        verified = [_verify_row(row, scope, gen, source) for row in rows]
    except IndexContractError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        _raise_stale("malformed search rows", exc)
    if _seal(index, token) != seal:
        _raise_stale("view seal changed during search")
    return verified


def search_collections_verified(index: Any, roots: Mapping[str, str], token: Any,
                                fts_query: str, limit: int, *,
                                collection_names: tuple[str, ...] | None = None,
                                max_files: int = 200,
                                max_bytes: int = 5_000_000,
                                wiki_only: bool = False) -> list[dict[str, Any]]:
    """Apply collection scope in SQL before LIMIT, then verify one pinned view."""
    limit = _positive_int("limit", limit)
    if collection_names is None:
        names = tuple(sorted(roots))
    else:
        if (not isinstance(collection_names, tuple) or not collection_names
                or any(not isinstance(name, str) or name not in roots
                       for name in collection_names)
                or len(set(collection_names)) != len(collection_names)):
            raise ValueError("invalid collection selection")
        names = collection_names
    seal = _seal(index, token)
    scope, gen, sealed_manifest, _ = seal
    source = scan_collections(roots, scope, index, max_files, max_bytes,
                              wiki_only=wiki_only)
    if sealed_manifest != source.manifest:
        _raise_stale("collection manifest differs from view seal")
    rows = index.search(token, fts_query, limit,
                        path_prefixes=tuple(name + "/" for name in names))
    try:
        rows = list(rows)
        if len(rows) > limit:
            raise ValueError("too many rows")
        verified = [_verify_row(row, scope, gen, source) for row in rows]
        if any(not any(row["path"].startswith(name + "/") for name in names)
               for row in verified):
            raise ValueError("out of selected collection")
    except IndexContractError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        _raise_stale("malformed collection search rows", exc)
    if _seal(index, token) != seal:
        _raise_stale("view seal changed during collection search")
    return verified
