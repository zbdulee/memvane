"""Laya-owned local embedding runtime: Node, node-llama-cpp, native build, GGUF.

The provider review reads its executable inventory from here, never from a
QMD package, a QMD runtime generation or the QMD model cache. Layout, all
owner-only (0700 directories, 0600/0700 files), no symlinks::

    ~/.local/state/laya/embed-runtime/
      .lock                  provisioning lock
      active.json            {"schema", "generation"}
      generations/g-<id>/    runtime.json, bin/node, node_modules/..., models/<gguf>

The root sits beside the install state roots and the per-user service under
the same private Laya state base. It is outside
``~/Library/Application Support/qmd-auto-context`` (the QMD runtime tree that
QMD removal deletes) and outside ``~/.cache`` (a cache may be purged; the
attested provider files must not be).

A generation is content addressed: ``<id>`` is the digest of its sealed
``runtime.json`` (every file's relative path, sha256, size and exec bit), so
the same bytes always land on the same absolute paths. ``select`` re-hashes
every file and refuses a missing, extra, changed or symlinked file.

Provisioning copies bytes into a private staging directory and renames it in
place. It never hardlinks: a second link would let the source tree (for
example QMD's npm) rewrite the attested bytes, and the attestation validator
refuses ``st_nlink != 1``. Sources, in order of use:

* an install's sealed provider attestation: the exact node, module entry,
  native closure and model files, each verified against the attested sha256;
* the QMD managed runtime generation and QMD model cache, read as plain
  files (``active.json`` → ``prepared.json``), with the pinned model sha256.

No source downloads anything. Without a source, provisioning refuses
``laya_embed_runtime_source_unavailable``.

``RUNTIME_ID`` keeps QMD's label: the frozen provider accepts only its known
runtime ids and binds the id into the embedding fingerprint. It names the
node-llama-cpp 3.18.1 build that the bytes are, not a QMD dependency.
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
QMD_MODEL_NAMES = ('hf_ggml-org_embeddinggemma-300M-Q8_0.gguf', MODEL_NAME)
NATIVE_SUFFIXES = ('.node', '.dylib', '.so')
MANIFEST = 'runtime.json'
ACTIVE = 'active.json'
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
    return Path(home or Path.home()) / '.local' / 'state' / 'laya' / 'embed-runtime'


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
    for name in ('.local', 'state', 'laya', 'embed-runtime'):
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


def select(home=None):
    """The active generation, fully re-verified; refuses otherwise."""
    runtime = _runtime_root(home)
    try:
        pointer, _raw = _read_json(runtime / ACTIVE, 4096)
    except FileNotFoundError as exc:
        raise RuntimeUnavailable('laya_embed_runtime_missing') from exc
    except OSError as exc:
        raise RuntimeUnavailable('laya_embed_runtime_corrupt') from exc
    if (not isinstance(pointer, dict) or set(pointer) != {'schema', 'generation'} or
            pointer['schema'] != SCHEMA or not isinstance(pointer['generation'], str) or
            not GENERATION.fullmatch(pointer['generation'])):
        _fail('laya_embed_runtime_corrupt')
    generation = runtime / 'generations' / pointer['generation']
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
    for path in (node, model):
        if not Path(path).is_absolute():
            _fail('laya_embed_runtime_source_unavailable')
        _no_symlink_ancestor(path)
    outer, module_pkg, native = _source_layout(module_entry)
    packages = _package_closure(module_pkg, outer.parent)
    if native not in packages:
        packages.append(native)
    runtime = _runtime_root(home, create=True)
    generations = runtime / 'generations'
    if not generations.exists() and not generations.is_symlink():
        os.mkdir(generations, 0o700)
    _owner_dir(generations)
    with _lock(runtime):
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
            _write_private(runtime / ACTIVE, _canonical({'schema': SCHEMA,
                                                         'generation': target.name}))
        except BaseException:
            if staging.exists():
                _remove_staging(staging)
            raise
    return select(home)


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
        _write_private(runtime / ACTIVE, _canonical({'schema': SCHEMA,
                                                     'generation': generation.name}))
    return select(home)


def qmd_sources(home=None):
    """Node, module entry and model of the QMD managed runtime, read as files."""
    home = Path(home or Path.home())
    qmd_root = home / 'Library' / 'Application Support' / 'qmd-auto-context' / 'runtimes' / 'qmd'
    try:
        pointer = json.loads((qmd_root / ACTIVE).read_text('utf-8'))
        generation = Path(pointer['generation'])
        if generation.parent != qmd_root / 'generations':
            _fail('laya_embed_runtime_source_unavailable')
        prepared = json.loads((generation / 'prepared.json').read_text('utf-8'))
        proof = prepared['proof']
        node, qmd = Path(proof['node']), Path(proof['qmd'])
        node_sha = proof['nodeSha256']
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeUnavailable('laya_embed_runtime_source_unavailable') from exc
    package = qmd.parent.parent
    candidates = [package / 'node_modules' / MODULE]
    if package.parent.name.startswith('@') and package.parent.parent.name == 'node_modules':
        candidates.append(package.parent.parent / MODULE)
    present = [path for path in candidates if (path / 'package.json').is_file()]
    if len(present) != 1:
        _fail('laya_embed_runtime_source_unavailable')
    models = home / '.cache' / 'qmd' / 'models'
    model = next((models / name for name in QMD_MODEL_NAMES
                  if (models / name).is_file()), None)
    if model is None:
        _fail('laya_embed_runtime_source_unavailable')
    return {'node': str(node), 'nodeSha256': node_sha,
            'moduleEntry': str(present[0] / 'dist' / 'index.js'), 'model': str(model)}


def provision_from_qmd(home=None, *, model_sha256=MODEL_SHA256):
    """Copy QMD's node-llama-cpp generation and cached GGUF (pinned sha256)."""
    sources = qmd_sources(home)
    return _provision(node=sources['node'], module_entry=sources['moduleEntry'],
                      model=sources['model'],
                      expect={'node': sources['nodeSha256'], 'model': model_sha256},
                      home=home)

