"""Laya-owned local embedding runtime: Node, node-llama-cpp, native build, GGUF.

The provider review reads its executable inventory from here. Layout, all
owner-only (0700 directories, 0600/0700 files), no symlinks::

    ~/.local/state/memvane/embed-runtime/
      .lock                  provisioning lock
      active.json            {"schema", "generation"}
      previous.json          the generation active.json named before the last switch
      generations/g-<id>/    runtime.json, bin/node, node_modules/..., models/<gguf>

The root sits beside the install state roots and the per-user service under
the same private MemVane state base, outside ``~/.cache`` (a cache may be purged;
the attested provider files must not be).

A generation is content addressed: ``<id>`` is the digest of its sealed
``runtime.json`` (every file's relative path, sha256, size and exec bit), so
the same bytes always land on the same absolute paths. ``select`` re-hashes
every file and refuses a missing, extra, changed or symlinked file.

Provisioning copies bytes into a private staging directory and renames it in
place. It never hardlinks: a second link would let the source tree rewrite
the attested bytes, and the attestation validator refuses ``st_nlink != 1``.
Sources:

* an install's sealed provider attestation: the exact node, module entry,
  native closure and model files, each verified against the attested sha256
  (``ensure_for_attestation``, run by the bundle upgrade worker);
* a new machine (``provision_new``, owner approved): Node from PATH or a
  version manager (supported majors only), ``npm ci --ignore-scripts`` of the
  pinned lock in ``locks/laya-embed-runtime``, and the GGUF from a local file
  or its pinned URL, accepted only with ``MODEL_SHA256``.

``collect`` removes generations that are neither active, previous, nor named
by an install attestation.

``RUNTIME_ID`` keeps its historical label: the frozen provider accepts only
its known runtime ids and binds the id into the embedding fingerprint. It
names the node-llama-cpp 3.18.1 build that the bytes are.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import uuid

SCHEMA = 'laya-embed-runtime-v1'
RUNTIME_ID = 'qmd-2.5.3-node-llama-cpp-3.18.1'
LLAMA_VERSION = '3.18.1'
MODULE = 'node-llama-cpp'
NATIVE = '@node-llama-cpp/mac-arm64-metal'
MODULE_ENTRY_TAIL = '/node-llama-cpp/dist/index.js'
NATIVE_PACKAGE_TAIL = '/@node-llama-cpp/mac-arm64-metal'
NODE = 'bin/node'
MODEL_NAME = 'embeddinggemma-300M-Q8_0.gguf'
MODEL = 'models/' + MODEL_NAME
# hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf
MODEL_SHA256 = 'f470220f84b6235197541352d22f10bf00098a8242c18eaacea9c8a4add557bc'
NATIVE_SUFFIXES = ('.node', '.dylib', '.so')
MANIFEST = 'runtime.json'
ACTIVE = 'active.json'
PREVIOUS = 'previous.json'
GENERATION = re.compile(r'g-[0-9a-f]{32}\Z')
SHA = re.compile(r'[0-9a-f]{64}\Z')
MAX_FILES = 50_000
MAX_PACKAGES = 2_000
FLAGS = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_CLOEXEC', 0)


class RuntimeUnavailable(RuntimeError):
    pass


def _fail(reason):
    raise RuntimeUnavailable(reason)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def root(home=None):
    import memvane_paths
    return memvane_paths.state_base(home or Path.home()) / 'embed-runtime'


def _owner_dir(path):
    info = Path(path).lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
            stat.S_IMODE(info.st_mode) & 0o077):
        _fail('laya_embed_runtime_unsafe')


def _account_home():
    import pwd
    return Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()


def _runtime_root(home=None, *, create=False):
    base = Path(home or Path.home())
    if not base.is_absolute() or str(base.resolve(strict=False)) != str(base):
        _fail('laya_embed_runtime_unsafe')
    if ((os.environ.get('QMD_SETUP_GUARD_FIXTURE') == '1' or os.environ.get('QMD_SANDBOX'))
            and base.resolve() == _account_home()):
        # Test and sandbox runs never read or write the account's real runtime.
        _fail('laya_embed_runtime_fixture_real_home')
    cursor = base
    import memvane_paths
    for name in (*memvane_paths.STATE_PARTS, 'embed-runtime'):
        cursor = cursor / name
        if not cursor.exists() and not cursor.is_symlink():
            if not create:
                _fail('laya_embed_runtime_missing')
            os.mkdir(cursor, 0o700)
        info = cursor.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                stat.S_IMODE(info.st_mode) & 0o022):
            _fail('laya_embed_runtime_unsafe')
    _owner_dir(cursor)
    return cursor


@contextmanager
def _lock(runtime):
    fd = os.open(runtime / '.lock', os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0),
                 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077):
            _fail('laya_embed_runtime_unsafe')
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeUnavailable('laya_embed_runtime_busy') from exc
        yield
    finally:
        os.close(fd)


def _hash(path, limit=None):
    """sha256 and size of one regular, non-symlinked file."""
    fd = os.open(path, FLAGS)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail('laya_embed_runtime_corrupt')
        digest = hashlib.sha256()
        while block := os.read(fd, 1024 * 1024):
            digest.update(block)
        after = os.fstat(fd)
        if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_ino, after.st_size, after.st_mtime_ns)):
            _fail('laya_embed_runtime_source_changed')
        return digest.hexdigest(), before.st_size
    finally:
        os.close(fd)


def _no_symlink_ancestor(path):
    cursor = Path('/')
    for part in Path(path).parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink():
            _fail('laya_embed_runtime_source_unsafe')


# ------------------------------------------------------------------ select

def _read_json(path, limit):
    fd = os.open(path, FLAGS)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or
                info.st_nlink != 1 or stat.S_IMODE(info.st_mode) & 0o077 or
                info.st_size > limit):
            _fail('laya_embed_runtime_corrupt')
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    try:
        return json.loads(raw.decode('utf-8')), raw
    except (UnicodeError, ValueError) as exc:
        raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc


def _manifest_valid(manifest):
    if (not isinstance(manifest, dict) or
            set(manifest) != {'schema', 'runtimeId', 'node', 'moduleEntry',
                              'nativePackage', 'model', 'files'} or
            manifest['schema'] != SCHEMA or manifest['runtimeId'] != RUNTIME_ID or
            manifest['node'] != NODE or manifest['model'] != MODEL or
            not _relative_ok(manifest['moduleEntry'], MODULE_ENTRY_TAIL) or
            not _relative_ok(manifest['nativePackage'], NATIVE_PACKAGE_TAIL) or
            not isinstance(manifest['files'], list) or
            not 0 < len(manifest['files']) <= MAX_FILES):
        return False
    seen = []
    for row in manifest['files']:
        if (not isinstance(row, list) or len(row) != 4 or
                not isinstance(row[0], str) or not isinstance(row[1], str) or
                not SHA.fullmatch(row[1]) or type(row[2]) is not int or row[2] < 0 or
                type(row[3]) is not bool or row[0] == MANIFEST or
                row[0].startswith('/') or '\\' in row[0] or '\x00' in row[0] or
                any(part in ('', '.', '..') for part in row[0].split('/'))):
            return False
        seen.append(row[0])
    names = set(seen)
    return (seen == sorted(names) and
            {NODE, manifest['moduleEntry'], MODEL} <= names and
            any(name.startswith(manifest['nativePackage'] + '/') for name in names))


def _relative_ok(value, tail):
    return (isinstance(value, str) and value.startswith('node_modules/') and
            value.endswith(tail) and '\\' not in value and '\x00' not in value and
            not any(part in ('', '.', '..') for part in value.split('/')))


def _verify_generation(generation):
    """Full re-hash of one sealed generation; returns its manifest."""
    _owner_dir(generation)
    manifest, raw = _read_json(generation / MANIFEST, 16 * 1024 * 1024)
    if (not _manifest_valid(manifest) or
            generation.name != 'g-' + hashlib.sha256(raw).hexdigest()[:32] or
            raw != _canonical(manifest)):
        _fail('laya_embed_runtime_corrupt')
    expected = {row[0]: row for row in manifest['files']}
    found = set()
    for directory, dirs, files in os.walk(generation, followlinks=False):
        here = Path(directory)
        for name in dirs:
            info = (here / name).lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or
                    stat.S_IMODE(info.st_mode) & 0o077):
                _fail('laya_embed_runtime_corrupt')
        for name in files:
            path = here / name
            rel = path.relative_to(generation).as_posix()
            if rel == MANIFEST:
                continue
            row = expected.get(rel)
            info = path.lstat()
            if (row is None or not stat.S_ISREG(info.st_mode) or
                    info.st_uid != os.getuid() or info.st_nlink != 1 or
                    stat.S_IMODE(info.st_mode) & 0o077 or
                    bool(stat.S_IMODE(info.st_mode) & 0o100) != row[3] or
                    info.st_size != row[2] or _hash(path)[0] != row[1]):
                _fail('laya_embed_runtime_corrupt')
            found.add(rel)
    if found != set(expected):
        _fail('laya_embed_runtime_corrupt')
    return manifest


def _described(generation, manifest):
    by_name = {row[0]: row for row in manifest['files']}
    native = sorted(row for row in manifest['files']
                    if row[0].startswith(manifest['nativePackage'] + '/') and
                    Path(row[0]).suffix in NATIVE_SUFFIXES)
    return {'schema': SCHEMA, 'generation': str(generation),
            'runtimeId': manifest['runtimeId'],
            'nodePath': str(generation / NODE), 'nodeSha256': by_name[NODE][1],
            'moduleEntryPath': str(generation / manifest['moduleEntry']),
            'moduleEntrySha256': by_name[manifest['moduleEntry']][1],
            'modulePackage': str((generation / manifest['moduleEntry']).parents[1]),
            'nativePackage': str(generation / manifest['nativePackage']),
            'modelPath': str(generation / MODEL), 'modelSha256': by_name[MODEL][1],
            'dependencyClosure': [{'path': str(generation / row[0]), 'sha256': row[1],
                                   'bytes': row[2]} for row in native]}


def _pointer(path, *, missing_ok=False):
    """The generation name a pointer file names, or None when absent and allowed."""
    try:
        pointer, _raw = _read_json(path, 4096)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RuntimeUnavailable('laya_embed_runtime_missing') from None
    except OSError as exc:
        raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc
    if (not isinstance(pointer, dict) or set(pointer) != {'schema', 'generation'} or
            pointer['schema'] != SCHEMA or not isinstance(pointer['generation'], str) or
            not GENERATION.fullmatch(pointer['generation'])):
        _fail('laya_embed_runtime_corrupt')
    return pointer['generation']


def _activate(runtime, name):
    """Point ``active.json`` at ``name``; the replaced one becomes ``previous.json``.

    ``active.json`` keeps its exact two-key shape, which older bundles verify.
    """
    try:
        current = _pointer(runtime / ACTIVE, missing_ok=True)
    except RuntimeUnavailable:
        current = None
    if current is not None and current != name:
        _write_private(runtime / PREVIOUS, _canonical({'schema': SCHEMA, 'generation': current}))
    _write_private(runtime / ACTIVE, _canonical({'schema': SCHEMA, 'generation': name}))


def select(home=None):
    """The active generation, fully re-verified; refuses otherwise."""
    runtime = _runtime_root(home)
    generation = runtime / 'generations' / _pointer(runtime / ACTIVE)
    try:
        _owner_dir(runtime / 'generations')
        manifest = _verify_generation(generation)
    except FileNotFoundError as exc:
        raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc
    except OSError as exc:
        raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc
    return _described(generation, manifest)


# ------------------------------------------------------------- provisioning

def _package_dir(base, name):
    return base.joinpath(*name.split('/'))


def _resolve(package, name, top):
    """Node's lookup: ``node_modules/<name>`` from ``package`` up to ``top``.

    ``top`` is the directory that holds the outermost ``node_modules``.
    """
    cursor = package
    while True:
        if cursor.name != 'node_modules':
            candidate = _package_dir(cursor / 'node_modules', name)
            if (candidate / 'package.json').is_file():
                return candidate
        if cursor == top:
            return None
        cursor = cursor.parent


def _package_closure(module_pkg, top):
    """node-llama-cpp plus every package its runtime imports resolve to."""
    found, queue = [], [module_pkg]
    while queue:
        package = queue.pop()
        if package in found:
            continue
        if len(found) >= MAX_PACKAGES or package.is_symlink():
            _fail('laya_embed_runtime_source_unsafe')
        found.append(package)
        try:
            meta = json.loads((package / 'package.json').read_text('utf-8'))
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeUnavailable('laya_embed_runtime_source_unavailable') from exc
        required = meta.get('dependencies') or {}
        optional = meta.get('optionalDependencies') or {}
        if not isinstance(required, dict) or not isinstance(optional, dict):
            _fail('laya_embed_runtime_source_unavailable')
        for name in sorted(set(required) | set(optional)):
            resolved = _resolve(package, name, top)
            if resolved is None:
                if name in required and name not in optional:
                    _fail('laya_embed_runtime_source_unavailable')
                continue
            queue.append(resolved)
    return found


def _source_layout(module_entry):
    """(outermost node_modules, module package, native package) for an entry.

    The copy keeps every package at its place relative to the outermost
    ``node_modules`` of the module's path (hoisted ``npm ci`` or nested
    under the QMD package alike), so Node resolves the copy as it did the
    source. Resolution never leaves that tree.
    """
    entry = Path(module_entry)
    if (not entry.is_absolute() or entry.name != 'index.js' or
            entry.parent.name != 'dist' or entry.parents[1].name != MODULE or
            entry.parents[2].name != 'node_modules'):
        _fail('laya_embed_runtime_source_unavailable')
    _no_symlink_ancestor(entry)
    module_pkg = entry.parents[1]
    outer = [parent for parent in module_pkg.parents if parent.name == 'node_modules'][-1]
    native = _resolve(module_pkg, NATIVE, outer.parent)
    if native is None:
        _fail('laya_embed_runtime_source_unavailable')
    for package in (module_pkg, native):
        try:
            version = json.loads((package / 'package.json').read_text('utf-8')).get('version')
        except (OSError, UnicodeError, ValueError, AttributeError) as exc:
            raise RuntimeUnavailable('laya_embed_runtime_source_unavailable') from exc
        if version != LLAMA_VERSION:
            _fail('laya_embed_runtime_source_version_changed')
    return outer, module_pkg, native


def _copy_file(source, target, executable):
    """Stream one regular file into a new private file; returns (sha, size)."""
    fd = os.open(source, FLAGS)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            _fail('laya_embed_runtime_source_unsafe')
        out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                      getattr(os, 'O_NOFOLLOW', 0), 0o700 if executable else 0o600)
        digest, size = hashlib.sha256(), 0
        try:
            while block := os.read(fd, 1024 * 1024):
                digest.update(block)
                size += len(block)
                view = memoryview(block)
                while view:
                    view = view[os.write(out, view):]
            os.fsync(out)
        finally:
            os.close(out)
        after = os.fstat(fd)
        if ((before.st_ino, before.st_size, before.st_mtime_ns) !=
                (after.st_ino, after.st_size, after.st_mtime_ns) or size != before.st_size):
            _fail('laya_embed_runtime_source_changed')
        return digest.hexdigest(), size
    finally:
        os.close(fd)


def _copy_package(package, outer, staging, rows):
    """Copy one package's own files (not its nested ``node_modules``)."""
    base = staging / 'node_modules' / package.relative_to(outer)
    for directory, dirs, files in os.walk(package, followlinks=False):
        here = Path(directory)
        if here == package:
            dirs[:] = [name for name in dirs if name != 'node_modules']
        else:
            dirs[:] = [name for name in dirs if name != '.bin']
        for name in dirs:
            if (here / name).is_symlink():
                _fail('laya_embed_runtime_source_unsafe')
        out_dir = base / here.relative_to(package)
        out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for name in files:
            source = here / name
            if source.is_symlink():
                _fail('laya_embed_runtime_source_unsafe')
            executable = bool(source.lstat().st_mode & 0o100)
            sha, size = _copy_file(source, out_dir / name, executable)
            rows.append([(out_dir / name).relative_to(staging).as_posix(), sha, size,
                         executable])
            if len(rows) > MAX_FILES:
                _fail('laya_embed_runtime_source_unsafe')


def _private_tree(path):
    for directory, dirs, _files in os.walk(path):
        for name in dirs:
            os.chmod(Path(directory) / name, 0o700)


def _remove_staging(path):
    if path.name.startswith('.staging-') and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


def _write_private(path, raw):
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                 0o600)
    try:
        os.write(fd, raw)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _provision(*, node, module_entry, model, expect, home=None):
    """Build, seal and activate one generation from exact source files.

    ``expect`` holds the digests the copy must reproduce: ``node``,
    ``moduleEntry``, ``model`` (sha256) and optionally ``closure`` (attested
    native rows, compared by relative layout, sha256 and size).
    """
    runtime = _runtime_root(home, create=True)
    generations = _generations(runtime)
    with _lock(runtime):
        _seal(runtime, generations, node=node, module_entry=module_entry, model=model,
              expect=expect)
    return select(home)


def _generations(runtime):
    generations = runtime / 'generations'
    if not generations.exists() and not generations.is_symlink():
        os.mkdir(generations, 0o700)
    _owner_dir(generations)
    return generations


def _seal(runtime, generations, *, node, module_entry, model, expect):
    """Copy, seal and activate one generation; the caller holds the lock."""
    for path in (node, model):
        if not Path(path).is_absolute():
            _fail('laya_embed_runtime_source_unavailable')
        _no_symlink_ancestor(path)
    outer, module_pkg, native = _source_layout(module_entry)
    packages = _package_closure(module_pkg, outer.parent)
    if native not in packages:
        packages.append(native)
    # A provisioner killed mid-copy leaves its staging tree; the lock
    # holder is the only writer, so every leftover is stale.
    for leftover in generations.glob('.staging-*'):
        _remove_staging(leftover)
    staging = generations / ('.staging-' + uuid.uuid4().hex)
    os.mkdir(staging, 0o700)
    try:
        rows = []
        (staging / 'bin').mkdir(mode=0o700)
        rows.append([NODE, *_copy_file(Path(node), staging / NODE, True), True])
        for package in sorted(packages):
            _copy_package(package, outer, staging, rows)
        (staging / 'models').mkdir(mode=0o700)
        rows.append([MODEL, *_copy_file(Path(model), staging / MODEL, False), False])
        _private_tree(staging)
        rows.sort()
        if len({row[0] for row in rows}) != len(rows):
            _fail('laya_embed_runtime_source_unsafe')
        manifest = {'schema': SCHEMA, 'runtimeId': RUNTIME_ID, 'node': NODE,
                    'moduleEntry': 'node_modules/' + (module_pkg / 'dist' / 'index.js')
                    .relative_to(outer).as_posix(),
                    'nativePackage': 'node_modules/' + native.relative_to(outer).as_posix(),
                    'model': MODEL, 'files': rows}
        if not _manifest_valid(manifest):
            _fail('laya_embed_runtime_source_unavailable')
        described = _described(staging, manifest)
        _check_expected(described, expect)
        raw = _canonical(manifest)
        _write_private(staging / MANIFEST, raw)
        target = generations / ('g-' + hashlib.sha256(raw).hexdigest()[:32])
        if target.exists() or target.is_symlink():
            try:
                _verify_generation(target)
            except (RuntimeUnavailable, OSError):
                _fail('laya_embed_runtime_generation_conflict')
            _remove_staging(staging)
        else:
            os.rename(staging, target)
        _activate(runtime, target.name)
    except BaseException:
        if staging.exists():
            _remove_staging(staging)
        raise


def _check_expected(described, expect):
    for key, field in (('node', 'nodeSha256'), ('moduleEntry', 'moduleEntrySha256'),
                       ('model', 'modelSha256')):
        if expect.get(key) is not None and described[field] != expect[key]:
            _fail('laya_embed_runtime_source_changed')
    if expect.get('closure') is not None:
        from laya_product_bundle import closure_layout
        if closure_layout(expect['closure']) != closure_layout(described['dependencyClosure']):
            _fail('laya_embed_runtime_source_changed')


def matches(runtime, manifest, closure):
    """True when ``runtime`` holds the same bytes an attestation names."""
    if not isinstance(runtime, dict) or not isinstance(manifest, dict):
        return False
    if any(runtime.get(key) != manifest.get(key) for key in
           ('nodeSha256', 'moduleEntrySha256', 'modelSha256', 'runtimeId')):
        return False
    from laya_product_bundle import closure_layout
    try:
        return closure_layout(closure) == closure_layout(runtime['dependencyClosure'])
    except Exception:
        return False


def provision_from_attestation(attestation, home=None):
    """Copy exactly the files an install's provider attestation names."""
    manifest = attestation.get('providerManifest') if isinstance(attestation, dict) else None
    closure = attestation.get('dependencyClosure') if isinstance(attestation, dict) else None
    if (not isinstance(manifest, dict) or manifest.get('runtimeId') != RUNTIME_ID or
            not isinstance(closure, list) or not closure):
        _fail('laya_embed_runtime_source_unavailable')
    paths = [manifest.get(key) for key in ('nodePath', 'moduleEntryPath', 'modelPath')]
    if not all(isinstance(path, str) and os.path.isfile(path) for path in paths) or not all(
            isinstance(row, dict) and isinstance(row.get('path'), str) and
            os.path.isfile(row['path']) for row in closure):
        _fail('laya_embed_runtime_source_unavailable')
    return _provision(node=manifest['nodePath'], module_entry=manifest['moduleEntryPath'],
                      model=manifest['modelPath'],
                      expect={'node': manifest.get('nodeSha256'),
                              'moduleEntry': manifest.get('moduleEntrySha256'),
                              'model': manifest.get('modelSha256'),
                              'closure': closure},
                      home=home)


def ensure_for_attestation(attestation, home=None):
    """The active runtime when it already holds the attested bytes; else copy them."""
    manifest = attestation.get('providerManifest') if isinstance(attestation, dict) else None
    closure = attestation.get('dependencyClosure') if isinstance(attestation, dict) else None
    try:
        current = select(home)
    except RuntimeUnavailable as exc:
        if str(exc) not in ('laya_embed_runtime_missing', 'laya_embed_runtime_corrupt'):
            raise
        current = None
    if current is not None and matches(current, manifest, closure):
        return current
    owned = _owned_generation(manifest, home)
    if owned is not None:
        # Already a Laya runtime: re-select that sealed generation after a
        # full check. Never copy out of it, or a changed dependency file that
        # the attestation does not hash would be sealed into a new generation.
        return _reactivate(owned, manifest, closure, home)
    try:
        return provision_from_attestation(attestation, home)
    except RuntimeUnavailable as exc:
        if current is not None and str(exc) == 'laya_embed_runtime_source_unavailable':
            # The attested sources are gone and the runtime holds other bytes.
            _fail('laya_embed_runtime_mismatch')
        raise


def _owned_generation(manifest, home):
    """The runtime generation an attested module entry lives in, if any."""
    entry = manifest.get('moduleEntryPath') if isinstance(manifest, dict) else None
    if not isinstance(entry, str):
        return None
    generations = root(home) / 'generations'
    try:
        relative = Path(entry).relative_to(generations)
    except ValueError:
        return None
    if not relative.parts or not GENERATION.fullmatch(relative.parts[0]):
        _fail('laya_embed_runtime_corrupt')
    return generations / relative.parts[0]


def _reactivate(generation, manifest, closure, home):
    runtime = _runtime_root(home)
    with _lock(runtime):
        try:
            _owner_dir(runtime / 'generations')
            described = _described(generation, _verify_generation(generation))
        except OSError as exc:
            raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc
        if not matches(described, manifest, closure):
            _fail('laya_embed_runtime_mismatch')
        _activate(runtime, generation.name)
    return select(home)


# ------------------------------------------------- new-machine provisioning

# Node majors the plugin supports (package.json ``engines``). A candidate is
# accepted only after its own ``--version`` passes this gate, so manager alias
# directories (``latest``, ``lts-*``) never select an unchecked binary.
MIN_NODE_MAJOR = 22
NODE_MAJORS = (22, 24, 26)
LOCK_DIR = Path(__file__).resolve().parents[1] / 'locks' / 'laya-embed-runtime'
MODEL_URL = ('https://huggingface.co/ggml-org/embeddinggemma-300M-GGUF/resolve/main/'
             + MODEL_NAME)
MAX_MODEL_BYTES = 2 * 1024 * 1024 * 1024
NPM_SECONDS = 1800
VERSION = re.compile(r'v?([0-9]+)\.([0-9]+)\.([0-9]+)\Z')
# Mach-O (both byte orders, 64/32-bit and fat) and ELF. A version-manager shim
# is a script; copying it would seal a launcher, not a Node binary.
BINARY_MAGIC = (b'\xcf\xfa\xed\xfe', b'\xce\xfa\xed\xfe', b'\xfe\xed\xfa\xcf',
                b'\xfe\xed\xfa\xce', b'\xca\xfe\xba\xbe', b'\x7fELF')


def _run(argv, *, cwd=None, env=None, timeout=60):
    import subprocess
    return subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout, check=False)


def _version_key(path):
    match = VERSION.fullmatch(path.name.removeprefix('node-'))
    return tuple(int(part) for part in match.groups()) if match else None


def _manager_nodes(home):
    """Node binaries of the known version managers, newest version first."""
    patterns = (
        ('.local/share/mise/installs/node', 'bin/node'),
        ('.local/share/fnm/node-versions', 'installation/bin/node'),
        ('Library/Application Support/fnm/node-versions', 'installation/bin/node'),
        ('.nvm/versions/node', 'bin/node'),
        ('.volta/tools/image/node', 'bin/node'),
        ('.asdf/installs/nodejs', 'bin/node'),
    )
    found = []
    for base, tail in patterns:
        directory = home / base
        try:
            versions = [path for path in directory.iterdir() if _version_key(path)]
        except OSError:
            continue
        for path in sorted(versions, key=_version_key, reverse=True):
            found.append(path / tail)
    found.extend(Path(path) for path in ('/opt/homebrew/bin/node', '/usr/local/bin/node',
                                         '/usr/bin/node'))
    return found


def node_candidates(*, path_env=None, home=None):
    """``node`` on PATH first, then version-manager installs, then system paths."""
    home = Path(home or Path.home())
    candidates = []
    found = shutil.which('node', path=path_env if path_env is not None
                         else os.environ.get('PATH', ''))
    if found:
        candidates.append(Path(found))
    candidates.extend(_manager_nodes(home))
    unique = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _binary(path):
    try:
        with open(path, 'rb') as stream:
            return stream.read(4) in BINARY_MAGIC
    except OSError:
        return False


def discover_node(*, path_env=None, home=None, run=_run):
    """(resolved node binary, version) of the first supported candidate."""
    seen = []
    for candidate in node_candidates(path_env=path_env, home=home):
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            continue
        if resolved in seen or not resolved.is_file() or not os.access(resolved, os.X_OK):
            continue
        seen.append(resolved)
        if not _binary(resolved):
            continue
        try:
            result = run([str(resolved), '--version'], timeout=15)
        except (OSError, ValueError):
            continue
        match = VERSION.fullmatch((result.stdout or '').strip())
        if result.returncode != 0 or match is None:
            continue
        major = int(match.group(1))
        if major >= MIN_NODE_MAJOR and major in NODE_MAJORS:
            return resolved, match.group(0).lstrip('v')
    _fail('laya_embed_runtime_node_unavailable')


def _npm_cli(node):
    """npm's own CLI script that ships beside this Node install."""
    cli = Path(node).parents[1] / 'lib' / 'node_modules' / 'npm' / 'bin' / 'npm-cli.js'
    if not cli.is_file():
        _fail('laya_embed_runtime_npm_unavailable')
    return cli


def _install_module(node, work, *, run=_run):
    """``npm ci`` of the pinned lock into ``work``; no lifecycle scripts."""
    for name in ('package.json', 'package-lock.json'):
        raw = (LOCK_DIR / name).read_bytes()
        _write_private(work / name, raw)
    cache = work / '.npm-cache'
    os.mkdir(cache, 0o700)
    env = {'PATH': str(Path(node).parent) + os.pathsep + '/usr/bin:/bin',
           'HOME': str(work), 'npm_config_cache': str(cache),
           'npm_config_update_notifier': 'false', 'npm_config_fund': 'false',
           'npm_config_audit': 'false', 'npm_config_ignore_scripts': 'true'}
    result = run([str(node), str(_npm_cli(node)), 'ci', '--ignore-scripts', '--omit=dev',
                  '--no-audit', '--no-fund'], cwd=str(work), env=env, timeout=NPM_SECONDS)
    if result.returncode != 0:
        raise RuntimeUnavailable('laya_embed_runtime_npm_failed')
    shutil.rmtree(cache, ignore_errors=True)
    entry = work / 'node_modules' / MODULE / 'dist' / 'index.js'
    if not entry.is_file():
        _fail('laya_embed_runtime_npm_failed')
    return entry


def download(url, target, *, limit=MAX_MODEL_BYTES):
    """Stream one HTTPS file into a new private file."""
    import urllib.request
    if not url.startswith('https://'):
        _fail('laya_embed_runtime_download_refused')
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0),
                 0o600)
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            written = 0
            while block := response.read(1024 * 1024):
                written += len(block)
                if written > limit:
                    _fail('laya_embed_runtime_download_too_large')
                view = memoryview(block)
                while view:
                    view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _fetch_model(work, *, model_file, model_sha256, approve_download, fetch):
    if model_file is not None:
        source = Path(model_file)
        if not source.is_absolute():
            _fail('laya_embed_runtime_source_unavailable')
        _no_symlink_ancestor(source)
        if _hash(source)[0] != model_sha256:
            _fail('laya_embed_runtime_model_sha_mismatch')
        return source
    if not approve_download:
        _fail('laya_embed_runtime_download_approval_required')
    target = work / MODEL_NAME
    try:
        fetch(MODEL_URL, target)
    except RuntimeUnavailable:
        raise
    except Exception as exc:
        raise RuntimeUnavailable('laya_embed_runtime_download_failed') from exc
    if _hash(target)[0] != model_sha256:
        target.unlink(missing_ok=True)
        _fail('laya_embed_runtime_model_sha_mismatch')
    return target


def provision_new(home=None, *, approve_download=False, model_file=None, node=None,
                  path_env=None, model_sha256=MODEL_SHA256, run=_run, fetch=download):
    """Build the runtime on a machine without any prior install.

    Node comes from PATH or a version manager (supported majors only); the
    pinned lock installs node-llama-cpp and its prebuilt native package with
    ``npm ci --ignore-scripts``; the GGUF is a given local file or the pinned
    download, accepted only with ``MODEL_SHA256``. Network access (npm
    registry, model host) needs ``approve_download``. The result is sealed and
    activated exactly like a copy from an attestation.
    """
    if not approve_download:
        _fail('laya_embed_runtime_download_approval_required')
    if node is None:
        node, _version = discover_node(path_env=path_env, home=home, run=run)
    runtime = _runtime_root(home, create=True)
    generations = _generations(runtime)
    with _lock(runtime):
        for leftover in runtime.glob('.provision-*'):
            _remove_work(leftover)
        work = runtime / ('.provision-' + uuid.uuid4().hex)
        os.mkdir(work, 0o700)
        try:
            entry = _install_module(node, work, run=run)
            model = _fetch_model(work, model_file=model_file, model_sha256=model_sha256,
                                 approve_download=approve_download, fetch=fetch)
            _seal(runtime, generations, node=node, module_entry=entry, model=model,
                  expect={'model': model_sha256})
        finally:
            _remove_work(work)
    return select(home)


def _remove_work(path):
    if path.name.startswith('.provision-') and path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)


# ------------------------------------------------------------------- gc


def _referenced_generations(home):
    """Generations a current install attestation still names."""
    import memvane_paths
    generations = root(home) / 'generations'
    named = set()
    installs = []
    # Installs not yet moved off the pre-rename state base still count.
    for base in (memvane_paths.state_base(home or Path.home()),
                 memvane_paths.legacy_state_base(home or Path.home())):
        try:
            installs.extend(path for path in base.iterdir() if path.is_dir())
        except FileNotFoundError:
            continue
    if not installs:
        return named, []
    unreadable = []
    for install in installs:
        path = install / 'production-provider-attestation.json'
        if not path.is_file():
            continue
        try:
            record = json.loads(path.read_text('utf-8'))
        except (OSError, UnicodeError, ValueError):
            unreadable.append(str(path))
            continue
        stack = [record]
        while stack:
            value = stack.pop()
            if isinstance(value, dict):
                # The historical profile closure rows are data, not files.
                stack.extend(item for key, item in value.items()
                             if key != 'profileDependencyClosure')
            elif isinstance(value, list):
                stack.extend(value)
            elif isinstance(value, str) and value.startswith(str(generations) + '/'):
                head = Path(value).relative_to(generations).parts[0]
                if GENERATION.fullmatch(head):
                    named.add(head)
    return named, unreadable


def collect(home=None, *, approve=False):
    """Remove generations other than active, previous and attested ones."""
    runtime = _runtime_root(home)
    generations = runtime / 'generations'
    with _lock(runtime):
        active = _pointer(runtime / ACTIVE)
        previous = _pointer(runtime / PREVIOUS, missing_ok=True)
        attested, unreadable = _referenced_generations(home)
        rows = []
        for path in sorted(generations.iterdir()):
            if path.name.startswith('.staging-'):
                rows.append({'generation': path.name, 'action': 'remove_stale_staging'})
            elif GENERATION.fullmatch(path.name) and path.is_dir() and not path.is_symlink():
                reason = ('active' if path.name == active else
                          'previous' if path.name == previous else
                          'attested' if path.name in attested else None)
                rows.append({'generation': path.name,
                             'action': 'keep' if reason else 'remove',
                             **({'reason': reason} if reason else {})})
            else:
                rows.append({'generation': path.name, 'action': 'keep',
                             'reason': 'unrecognized'})
        if unreadable:
            # An unreadable attestation may name any generation.
            return {'status': 'blocked', 'reason': 'attestation_unreadable',
                    'attestations': unreadable, 'generations': rows, 'changesApplied': 0}
        removed = 0
        if approve:
            for row in rows:
                path = generations / row['generation']
                if row['action'] == 'remove_stale_staging':
                    _remove_staging(path)
                elif row['action'] == 'remove':
                    _owner_dir(path)
                    shutil.rmtree(path)
                else:
                    continue
                row['action'] = 'removed'
                removed += 1
    return {'status': 'collected' if approve else 'planned', 'active': active,
            'previous': previous, 'generations': rows, 'changesApplied': removed}
