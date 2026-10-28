#!/usr/bin/env python3
"""Journal v4 and provider re-proof: QMD-free selected routes.

Synthetic HOME, temp state roots and temp host caches only. No real project,
``~/.local/state/laya``, ``~/.cache/qmd`` or ``~/.config/qmd`` is read or
written; the "global QMD" directories below live in the temp HOME.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
CORE = FIXTURES.parents[1] / 'core'
sys.path.insert(0, str(FIXTURES))
sys.path.insert(0, str(CORE))
from laya_selected_project import SelectedProject  # noqa: E402
import laya_bundle_upgrade as auto  # noqa: E402
import laya_install_journal as J  # noqa: E402
import laya_product_bundle as bundle  # noqa: E402

V3_CREATE = J.__dict__.get('_CREATE_V3', J._CREATE)
V3_FIELDS = ('id', 'schema', 'epoch', 'state', 'selected', 'selected_route_sha',
             'previous', 'previous_route_sha', 'pending', 'pending_route_sha',
             'scope_id', 'project_root', 'project_dev', 'project_ino',
             'user_qmd_inventory', 'new_write_seen', 'provider_attestation_sha',
             'previous_provider_attestation_sha', 'provider_file_identity',
             'previous_provider_file_identity')
STEPS = ('locked', 'dropped', 'created', 'inserted', 'versioned', 'committed')


def canon(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def write_v3(journal, inventory):
    """Rewrite a journal exactly as 0.38.0 stores it (v3, QMD inventory)."""
    with closing(sqlite3.connect(journal, isolation_level=None)) as db:
        names = [r[1] for r in db.execute('PRAGMA table_info(selection)')]
        values = dict(zip(names, db.execute('SELECT * FROM selection').fetchone()))
        values['schema'] = 'laya-installer-journal-v3'
        if values['state'] == 'restored_origin':
            values['state'] = 'restored_qmd'
        values['user_qmd_inventory'] = canon(inventory)
        db.execute('BEGIN IMMEDIATE')
        db.execute('DROP TABLE selection')
        db.execute(V3_CREATE)
        db.execute('INSERT INTO selection VALUES(' + ','.join('?' * 20) + ')',
                   tuple(values[key] for key in V3_FIELDS))
        db.execute('PRAGMA user_version=3')
        db.execute('COMMIT')


def stored(journal):
    with closing(sqlite3.connect(journal, isolation_level=None)) as db:
        return (db.execute('PRAGMA user_version').fetchone()[0],
                db.execute("SELECT sql FROM sqlite_master WHERE name='selection'").fetchone()[0],
                [r[1] for r in db.execute('PRAGMA table_info(selection)')])


class V3Selected(SelectedProject):
    """A selected install whose journal is the 0.38.0 v3 format."""

    def setUp(self):
        super().setUp()
        self.qmd_cache = self.home / '.cache' / 'qmd'
        self.qmd_config = self.home / '.config' / 'qmd'
        for path in (self.qmd_cache / 'models', self.qmd_config):
            path.mkdir(parents=True)
        inventory = [{'root': str(path), 'dev': path.stat().st_dev, 'ino': path.stat().st_ino}
                     for path in sorted((self.qmd_cache, self.qmd_config, self.user_qmd))]
        write_v3(self.journal, inventory)
        self.inventory = inventory
        self.assertEqual(stored(self.journal)[0], 3)

    def remove_global_qmd(self):
        shutil.rmtree(self.qmd_cache)
        os.rename(self.qmd_config, self.home / '.config' / 'qmd-moved')
        shutil.rmtree(self.user_qmd)

    def full_proof(self, decision):
        selected = J.inspect_journal(self.journal)['selected']
        rows = [[str(self.plugin / 'p.py'), 1, 2, 3, 4, 5]]
        with mock.patch.object(J, '_inspect_identity', return_value=self.proof), \
             mock.patch.object(J, '_verified_executing_root',
                               return_value=selected['pluginRoot']), \
             mock.patch.object(J, '_provider_file_identity', return_value=rows):
            return J.verify_selected_route(decision)


class RecallWithoutGlobalQmd(V3Selected):
    def test_v3_route_serves_after_global_qmd_dirs_are_gone(self):
        self.remove_global_qmd()
        decision, _proof = self.assert_selected_and_readable()
        self.assertTrue(self.full_proof(decision)['productRouteAllowed'])
        out, calls = self.serve('recall', decision, {'prompt': 'x'})
        self.assertEqual(out, {'served': True})
        # No plugin cache is built here: recall serves the read-only proof.
        self.assertEqual(calls, [{'state': 'candidate', 'degraded': True,
                                  'readOnly': True}])

    def test_v4_route_serves_after_global_qmd_dirs_are_gone(self):
        J.convert_to_v4(self.journal)
        self.remove_global_qmd()
        decision, _proof = self.assert_selected_and_readable()
        self.assertTrue(self.full_proof(decision)['productRouteAllowed'])
        out, _calls = self.serve('recall', decision, {'prompt': 'x'})
        self.assertEqual(out, {'served': True})

    def test_new_code_cas_keeps_a_v3_journal_byte_compatible(self):
        epoch = J.inspect_journal(self.journal)['epoch']
        J.cas_mark_new_write(self.journal, epoch)
        version, sql, columns = stored(self.journal)
        self.assertEqual((version, sql), (3, V3_CREATE))
        self.assertIn('user_qmd_inventory', columns)
        with closing(sqlite3.connect(self.journal)) as db:
            kept = db.execute('SELECT user_qmd_inventory, schema FROM selection').fetchone()
        self.assertEqual(json.loads(kept[0]), self.inventory)
        self.assertEqual(kept[1], 'laya-installer-journal-v3')
        self.assertTrue(J.inspect_journal(self.journal)['newWriteSeen'])


class Conversion(V3Selected):
    def test_conversion_keeps_the_logical_row_and_reader_proof(self):
        decision, before = self.assert_selected_and_readable()
        inspected = J.inspect_journal(self.journal)
        result = J.convert_to_v4(self.journal)
        self.assertEqual(result['status'], 'journal_v4_converted')
        version, sql, columns = stored(self.journal)
        self.assertEqual((version, sql), (4, J._CREATE))
        self.assertNotIn('user_qmd_inventory', columns)
        self.assertEqual(J.inspect_journal(self.journal), inspected)
        self.assertEqual(J.verify_selected_route_read_only(decision), before)
        self.assertEqual(J.convert_to_v4(self.journal)['status'], 'journal_v4_current')

    def test_withdrawn_state_is_renamed_and_read_as_restored_origin(self):
        with closing(sqlite3.connect(self.journal, isolation_level=None)) as db:
            db.execute("UPDATE selection SET state='restored_qmd', selected=NULL, "
                       "selected_route_sha=NULL, pending=NULL, pending_route_sha=NULL, "
                       "provider_attestation_sha=NULL, provider_file_identity=NULL")
        self.assertEqual(J.inspect_journal(self.journal)['state'], 'restored_origin')
        J.convert_to_v4(self.journal)
        with closing(sqlite3.connect(self.journal)) as db:
            self.assertEqual(db.execute('SELECT state FROM selection').fetchone()[0],
                             'restored_origin')
        self.assertEqual(J.inspect_journal(self.journal)['state'], 'restored_origin')

    def crash_at(self, step):
        script = ('import os, sys; sys.path.insert(0, sys.argv[1]); '
                  'import laya_install_journal as J; '
                  'J._conversion_step = lambda name: os._exit(9) if name == sys.argv[3] else None; '
                  'J.convert_to_v4(sys.argv[2]); os._exit(0)')
        done = subprocess.run([sys.executable, '-B', '-c', script, str(CORE),
                               str(self.journal), step],
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 9, done.stderr)

    def test_hot_journal_of_a_crashed_writer_does_not_stop_readers(self):
        decision, before = self.assert_selected_and_readable()
        script = ('import os, sqlite3, sys; '
                  'db = sqlite3.connect(sys.argv[1], isolation_level=None); '
                  "db.execute('PRAGMA cache_size=1'); db.execute('BEGIN IMMEDIATE'); "
                  "db.execute('DROP TABLE selection'); db.execute('CREATE TABLE junk(x)'); "
                  "[db.execute('INSERT INTO junk VALUES(randomblob(4000))') for _ in range(300)]; "
                  'os._exit(9)')
        done = subprocess.run([sys.executable, '-B', '-c', script, str(self.journal)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 9, done.stderr)
        side = Path(str(self.journal) + '-journal')
        self.assertTrue(side.exists())
        uri = Path(self.journal).as_uri() + '?mode=ro'
        with self.assertRaises(sqlite3.OperationalError):
            with closing(sqlite3.connect(uri, uri=True)) as raw:
                raw.execute('SELECT count(*) FROM selection').fetchone()
        self.assertEqual(J.verify_selected_route_read_only(decision), before)
        self.assertFalse(side.exists())
        self.assertEqual(J.journal_version(self.journal), 3)

    def test_crash_at_each_step_leaves_one_readable_format_and_resumes(self):
        original = Path(self.journal).read_bytes()
        decision, before = self.assert_selected_and_readable()
        inspected = J.inspect_journal(self.journal)
        for step in STEPS:
            with self.subTest(step=step):
                Path(self.journal).write_bytes(original)
                side = Path(str(self.journal) + '-journal')
                side.unlink(missing_ok=True)
                self.crash_at(step)
                expected = 4 if step == 'committed' else 3
                # A read-only reader first: the hot journal must not stop search.
                self.assertEqual(J.verify_selected_route_read_only(decision), before)
                self.assertEqual(J.journal_version(self.journal), expected)
                self.assertEqual(J.inspect_journal(self.journal), inspected)
                J.convert_to_v4(self.journal)
                self.assertEqual(stored(self.journal)[:2], (4, J._CREATE))
                self.assertEqual(J.inspect_journal(self.journal), inspected)
                self.assertEqual(J.verify_selected_route_read_only(decision), before)


class HostGate(V3Selected):
    def setUp(self):
        super().setUp()
        # Each test names its own Hermes home; never the session's.
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('HERMES_HOME', None)
        self.cache = self.home / '.claude' / 'plugins' / 'cache' / 'mkt' / 'qmd-auto-context'
        self.claude = self.copy(self.cache / '0.38.0', 4)
        self.codex = self.copy(self.home / '.codex' / 'plugins' / 'cache' / 'mkt' /
                               'qmd-auto-context' / '0.38.0', 3)
        record = self.home / '.claude' / 'plugins' / 'installed_plugins.json'
        record.write_text(json.dumps({'version': 2, 'plugins': {
            'qmd-auto-context@mkt': [{'installPath': str(self.claude)}]}}))
        # Another marketplace's stale copy never gates this plugin.
        self.copy(self.home / '.codex' / 'plugins' / 'cache' / 'other' /
                  'qmd-auto-context' / '0.35.2', 3)

    @staticmethod
    def copy(root, version):
        (root / 'core').mkdir(parents=True)
        (root / 'core' / 'laya_install_journal.py').write_text(
            f"SCHEMA = 'x'\nVERSION = {version}\n")
        return root

    def convert(self):
        import laya_journal_v4
        return laya_journal_v4.convert_install(self.state, self.project,
                                               root=self.claude, home=self.home)

    def test_conversion_waits_for_every_host_copy_to_read_v4(self):
        import laya_journal_v4
        self.assertEqual(self.convert(), {'status': 'journal_v4_waiting',
                                          'reason': 'journal_v4_waiting_hosts'})
        self.assertEqual(stored(self.journal)[0], 3)
        self.assertEqual(laya_journal_v4.hosts_ready(Path('/elsewhere'), self.home),
                         (False, 'host_layout_unknown'))
        self.copy_version(self.codex, 4)
        self.assertEqual(self.convert()['status'], 'journal_v4_converted')
        self.assertEqual(stored(self.journal)[0], 4)
        self.assertEqual(self.convert(), {'status': 'journal_v4_current'})

    @staticmethod
    def copy_version(root, version):
        (root / 'core' / 'laya_install_journal.py').write_text(f'VERSION = {version}\n')

    def test_conversion_waits_for_the_hermes_copy(self):
        import laya_journal_v4
        self.copy_version(self.codex, 4)
        plugins = self.home / '.hermes' / 'plugins'
        hermes = self.copy(plugins / 'qmd-auto-context', 3)
        self.assertIn(str(hermes), laya_journal_v4.host_copies(self.claude, self.home))
        self.assertEqual(self.convert(), {'status': 'journal_v4_waiting',
                                          'reason': 'journal_v4_waiting_hosts'})
        self.assertEqual(stored(self.journal)[0], 3)
        # A renamed directory is still found by the name its plugin.yaml declares.
        renamed = plugins / 'laya-memory'
        shutil.move(str(hermes), str(renamed))
        (renamed / 'plugin.yaml').write_text('name: qmd-auto-context\nversion: "0.39.0"\n')
        self.assertEqual(self.convert()['reason'], 'journal_v4_waiting_hosts')
        # An unreadable Hermes plugin directory cannot be proven: hold.
        plugins.chmod(0)
        try:
            self.assertEqual(laya_journal_v4.hosts_ready(self.claude, self.home),
                             (False, 'hermes_install_unreadable'))
        finally:
            plugins.chmod(0o755)
        # A comment after the name, and an unprovable manifest that holds.
        (renamed / 'plugin.yaml').write_text('name: qmd-auto-context # renamed copy\n')
        self.assertEqual(self.convert()['reason'], 'journal_v4_waiting_hosts')
        other = plugins / 'other-plugin'
        other.mkdir()
        for unprovable in ('version: 1\n', 'name: |\n  qmd-auto-context\n'):
            (other / 'plugin.yaml').write_text(unprovable)
            self.assertEqual(laya_journal_v4.hosts_ready(self.claude, self.home),
                             (False, 'hermes_install_unprovable'))
        (other / 'plugin.yaml').write_bytes(b'name: \xff\n')
        self.assertEqual(laya_journal_v4.hosts_ready(self.claude, self.home),
                         (False, 'hermes_install_unreadable'))
        self.copy_version(renamed, 4)
        # Quoted escapes are not interpreted: a v3 copy whose name is spelled
        # with one (it may be this plugin) holds instead of being skipped.
        self.copy(other, 3)
        for escaped in ('name: "qmd\\x2dauto-context"\n', "name: 'qmd''auto'\n"):
            (other / 'plugin.yaml').write_text(escaped)
            self.assertEqual(laya_journal_v4.hosts_ready(self.claude, self.home),
                             (False, 'hermes_install_unprovable'))
            self.assertEqual(stored(self.journal)[0], 3)
        (other / 'plugin.yaml').write_text('name: "other-plugin"  # unrelated\n')
        self.assertEqual(self.convert()['status'], 'journal_v4_converted')
        self.assertEqual(stored(self.journal)[0], 4)

    def test_conversion_waits_for_a_copy_under_a_custom_hermes_home(self):
        import laya_journal_v4
        self.copy_version(self.codex, 4)
        custom = self.home / 'hermes-elsewhere'
        hermes = self.copy(custom / 'plugins' / 'qmd-auto-context', 3)
        os.environ['HERMES_HOME'] = str(custom)
        self.assertIn(str(hermes), laya_journal_v4.host_copies(self.claude, self.home))
        self.assertEqual(self.convert(), {'status': 'journal_v4_waiting',
                                          'reason': 'journal_v4_waiting_hosts'})
        self.assertEqual(stored(self.journal)[0], 3)
        # The detached upgrade worker that converts journals keeps it too.
        spawned = []
        with mock.patch.object(auto.subprocess, 'Popen',
                               side_effect=lambda argv, **kw: spawned.append(kw['env'])):
            auto._spawn(self.project)
        self.assertEqual(spawned[0].get('HERMES_HOME'), str(custom))
        self.copy_version(hermes, 4)
        self.assertEqual(self.convert()['status'], 'journal_v4_converted')

    def test_busy_install_lock_defers_without_converting(self):
        import laya_journal_v4
        from laya_product_install import _install_lock
        self.copy_version(self.codex, 4)
        with _install_lock(self.state):
            with self.assertRaisesRegex(RuntimeError, 'journal_v4_install_busy'):
                self.convert()
        self.assertIn(laya_journal_v4.BUSY, auto.DEFERRALS)
        self.assertEqual(stored(self.journal)[0], 3)

    def test_worker_converts_after_a_current_bundle_step(self):
        self.copy_version(self.codex, 4)
        with mock.patch.object(auto, 'installs', return_value=[self.state]), \
             mock.patch.object(auto, 'upgrade_install', return_value={'status': 'current'}):
            results = auto.run(self.project, root=self.claude, home=self.home)
        self.assertEqual(results[0]['status'], 'current')
        self.assertEqual(stored(self.journal)[0], 4)
        self.assertIn('journal journal_v4_converted',
                      (self.state / auto.LOG).read_text())

    def test_selected_session_start_requests_conversion_only_for_v3(self):
        requested = []
        decision = self.decision()
        with mock.patch.object(auto, 'request',
                               side_effect=lambda *a, **k: requested.append(a)):
            import laya_host_response as response
            response._request_journal_v4(decision)
            J.convert_to_v4(self.journal)
            response._request_journal_v4(decision)
        self.assertEqual(requested, [(decision['projectRoot'], decision['journalPath'],
                                      'journal_v4_conversion')])


class SchemaSnapshot(V3Selected):
    def test_schema_version_reads_one_snapshot_while_conversion_commits(self):
        journal = Path(self.journal)
        outcome = []

        def convert():
            try:
                outcome.append(J.convert_to_v4(journal)['status'])
            except Exception as exc:  # noqa: BLE001 - asserted below
                outcome.append(exc)

        class Rows:
            def __init__(self, rows):
                self.rows = rows

            def fetchone(self):
                return self.rows[0] if self.rows else None

        class Racing:
            """Runs a whole v3->v4 conversion between the two schema reads."""

            def __init__(self, db):
                self.db, self.thread = db, None

            @property
            def in_transaction(self):
                return self.db.in_transaction

            def execute(self, sql, *args):
                cursor = self.db.execute(sql, *args)
                if sql == 'PRAGMA user_version' and self.thread is None:
                    rows = cursor.fetchall()
                    self.thread = threading.Thread(target=convert)
                    self.thread.start()
                    self.thread.join(1.0)  # a reader holding its snapshot blocks COMMIT
                    return Rows(rows)
                return cursor

            def commit(self):
                self.db.commit()

            def rollback(self):
                self.db.rollback()
        with closing(sqlite3.connect(journal.as_uri() + '?mode=ro', uri=True, timeout=2,
                                     isolation_level=None)) as db:
            racing = Racing(db)
            version = J._schema_version(racing)
            racing.thread.join(10)
        self.assertEqual(version, 3, 'v3 user_version paired with v4 table SQL')
        self.assertEqual(outcome, ['journal_v4_converted'])
        self.assertEqual(J.journal_version(journal), 4)


class NewJournals(SelectedProject):
    def test_new_journal_is_v4_and_needs_no_qmd_roots(self):
        version, sql, columns = stored(self.journal)
        self.assertEqual((version, sql), (4, J._CREATE))
        self.assertNotIn('user_qmd_inventory', columns)


# ------------------------------------------------------------- provider re-proof

def sha_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def seal_generation(tree, home):
    """Seal ``tree`` as a Laya runtime generation under ``home``; returns its path.

    Same bytes give the same content-addressed name under any home.
    """
    import laya_embed_runtime as R
    rows = []
    for path in sorted(p for p in tree.rglob('*') if p.is_file()):
        executable = bool(path.stat().st_mode & 0o100)
        rows.append([path.relative_to(tree).as_posix(), sha_file(path),
                     path.stat().st_size, executable])
    manifest = {'schema': R.SCHEMA, 'runtimeId': R.RUNTIME_ID, 'node': R.NODE,
                'moduleEntry': 'node_modules/node-llama-cpp/dist/index.js',
                'nativePackage': 'node_modules/@node-llama-cpp/mac-arm64-metal',
                'model': R.MODEL, 'files': rows}
    raw = R._canonical(manifest)
    (tree / R.MANIFEST).write_bytes(raw)
    (tree / R.MANIFEST).chmod(0o600)
    generations = home / '.local' / 'state' / 'laya' / 'embed-runtime' / 'generations'
    generations.mkdir(parents=True, exist_ok=True)
    for path in (generations, *[d for d in tree.rglob('*') if d.is_dir()], tree):
        path.chmod(0o700)
    target = generations / ('g-' + hashlib.sha256(raw).hexdigest()[:32])
    os.rename(tree, target)
    return target


class ReproofBase(unittest.TestCase):
    """Real files, the real validator and the frozen provider's manifest check.

    The old provider runs from a sealed Laya runtime generation, the shape of
    every existing install; ``moved`` puts the same bytes under another home.
    """

    def setUp(self):
        from laya_index_vector import profile_identity
        from laya_local_embed_provider import DEFAULT_EMBED_MODEL_URI, recipe_fingerprint
        # Short base: the provider socket path is limited to 100 bytes.
        tmp = tempfile.TemporaryDirectory(prefix='lr-', dir='/tmp')
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name).resolve()
        self.root = self.base / 'plugin'
        (self.root / 'core').mkdir(parents=True)
        for name in ('laya_local_embed_provider.py', 'laya_local_embed_worker.mjs'):
            (self.root / 'core' / name).write_bytes((CORE / name).read_bytes())
        self.state = self.base / 'state'
        (self.state / 'runtime').mkdir(parents=True, mode=0o700)
        self.state.chmod(0o700)
        self.old = self.base / 'staging'
        self.files = {'node': 'bin/node', 'module': 'node_modules/node-llama-cpp/dist/index.js',
                      'model': 'models/embeddinggemma-300M-Q8_0.gguf'}
        for key, rel in self.files.items():
            (self.old / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.old / rel).write_bytes(b'GGUF' + key.encode() * 64)
        native = self.old / 'node_modules' / '@node-llama-cpp' / 'mac-arm64-metal' / 'bins'
        native.mkdir(parents=True)
        (native / 'llama-addon.node').write_bytes(b'addon' * 100)
        (native / 'libggml.dylib').write_bytes(b'ggml' * 100)
        relative = native.relative_to(self.old)
        self.old = seal_generation(self.old, self.base / 'h1')
        native = self.native = self.old / relative
        closure = self.closure(native)
        closure_sha = hashlib.sha256(bundle._canonical(closure)).hexdigest()
        provider_sha = sha_file(self.root / 'core' / 'laya_local_embed_provider.py')
        worker_sha = sha_file(self.root / 'core' / 'laya_local_embed_worker.mjs')
        self.content = dict(
            model_id=DEFAULT_EMBED_MODEL_URI,
            model_sha256=sha_file(self.old / self.files['model']),
            runtime_id='qmd-2.5.3-node-llama-cpp-3.18.1',
            recipe_id='qmd-regex-token-chunks-candidate-v1',
            provider_sha256=provider_sha, worker_sha256=worker_sha,
            node_sha256=sha_file(self.old / self.files['node']),
            module_entry_sha256=sha_file(self.old / self.files['module']))
        self.recipe_fingerprint, self.profile_identity = recipe_fingerprint, profile_identity
        self.old_att = self.attestation(self.old, closure, self.state / 'runtime' /
                                        'laya-embed.sock')
        self.assertEqual(self.old_att['providerManifest']['dependencyClosureSha256'],
                         closure_sha)

    @staticmethod
    def closure(native):
        return [{'path': str(path), 'sha256': sha_file(path), 'bytes': path.stat().st_size}
                for path in sorted(native.iterdir())]

    def review(self, base, closure, socket, **content):
        """What ``review_local_provider`` would propose for files under ``base``."""
        dimension = content.pop('dimension', 768)
        values = {**self.content, **content}
        closure_sha = hashlib.sha256(bundle._canonical(closure)).hexdigest()
        fingerprint = self.recipe_fingerprint(**values, closure_sha256=closure_sha)
        approved = 'local-qmd-' + hashlib.sha256(
            (values['provider_sha256'] + values['worker_sha256'] + closure_sha)
            .encode()).hexdigest()[:24]
        profile = {'profileId': self.profile_identity(values['model_id'], fingerprint,
                                                      dimension),
                   'modelId': values['model_id'], 'embeddingFingerprint': fingerprint,
                   'dimension': dimension, 'metric': 'cosine',
                   'approvedProviderId': approved,
                   'providerArtifactSha256': values['provider_sha256'],
                   'providerRelativePath': 'core/laya_local_embed_provider.py'}
        manifest = {'approvedProviderId': approved,
                    'providerArtifactSha256': values['provider_sha256'],
                    'providerRelativePath': 'core/laya_local_embed_provider.py',
                    'providerPath': str(self.root / 'core/laya_local_embed_provider.py'),
                    'workerPath': str(self.root / 'core/laya_local_embed_worker.mjs'),
                    'workerSha256': values['worker_sha256'],
                    'serviceSocketPath': str(socket),
                    'nodePath': str(base / self.files['node']),
                    'nodeSha256': values['node_sha256'],
                    'moduleEntryPath': str(base / self.files['module']),
                    'moduleEntrySha256': values['module_entry_sha256'],
                    'modelPath': str(base / self.files['model']),
                    'modelSha256': values['model_sha256'],
                    'runtimeId': values['runtime_id'], 'modelId': values['model_id'],
                    'embeddingFingerprint': fingerprint, 'dimension': dimension,
                    'recipeId': values['recipe_id'], 'dependencyClosureApproved': True,
                    'dependencyClosureSha256': closure_sha}
        return {'proposalSha256': '9' * 64, 'reviewedProvider': {
            'vectorProfile': profile, 'providerManifest': manifest,
            'dependencyClosure': closure}}

    def attestation(self, base, closure, socket):
        reviewed = self.review(base, closure, socket)['reviewedProvider']
        return {'schema': bundle.PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
                'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                'journalPath': str(self.state / 'install-journal.sqlite'),
                'vectorProfile': reviewed['vectorProfile'],
                'providerManifest': reviewed['providerManifest'],
                'dependencyClosure': reviewed['dependencyClosure']}

    def proof(self, profile):
        return {'status': 'exact_commit_full_plugin_candidate_consistent',
                'pluginRoot': str(self.root), 'sourceCommit': 'c' * 40,
                'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                'vectorProfile': profile}

    def validate(self, attestation):
        proof = self.proof(attestation['vectorProfile'])
        with mock.patch.object(bundle, 'inspect_full_plugin_candidate', return_value=proof):
            return bundle.validate_production_provider_attestation(
                attestation['vectorProfile'], proof, attestation)

    def moved(self):
        """Same bytes in another home's generation and a per-user service socket."""
        generations = self.base / 'h2' / '.local' / 'state' / 'laya' / 'embed-runtime'
        new = generations / 'generations' / self.old.name
        new.parent.mkdir(parents=True)
        new.parent.chmod(0o700)
        shutil.copytree(self.old, new)
        service = self.base / 'service'
        service.mkdir(mode=0o700)
        return new, self.closure(new / self.native.relative_to(self.old)), service / 'laya.sock'

    def unpinned(self):
        """The same bytes outside any Laya generation (a QMD-era runtime tree)."""
        plain = self.base / 'qmd-pkg'
        shutil.copytree(self.old, plain)
        (plain / 'runtime.json').unlink()
        return self.attestation(plain, self.closure(plain / self.native.relative_to(self.old)),
                                self.state / 'runtime' / 'laya-embed.sock')


class ProviderReproof(ReproofBase):
    def test_old_attestation_is_valid(self):
        self.validate(self.old_att)

    def test_moved_runtime_model_socket_and_closure_reattest_under_old_profile(self):
        from laya_local_embed_provider import ReviewedLocalQmdProvider
        from laya_provider_reproof import attestation_record, reattest
        new, closure, socket = self.moved()
        review = self.review(new, closure, socket)
        # A moved closure changes the path-derived profile the review proposes.
        self.assertNotEqual(review['reviewedProvider']['vectorProfile'],
                            self.old_att['vectorProfile'])
        self.assertTrue(auto.provider_equivalent(self.old_att, review, self.root))
        again = reattest(self.old_att, review['reviewedProvider'])
        self.assertEqual(again['vectorProfile'], self.old_att['vectorProfile'])
        self.assertEqual(again['profileDependencyClosure'],
                         self.old_att['dependencyClosure'])
        manifest = again['providerManifest']
        self.assertEqual(manifest['modelPath'], str(new / self.files['model']))
        self.assertEqual(manifest['serviceSocketPath'], str(socket))
        record = attestation_record(proof=self.proof(again['vectorProfile']),
                                    journal_path=self.state / 'install-journal.sqlite',
                                    reattested=again,
                                    schema=bundle.PRODUCTION_ATTESTATION_SCHEMA)
        self.validate(record)
        # The frozen provider accepts the re-attested manifest unchanged.
        provider = ReviewedLocalQmdProvider(manifest)
        self.assertEqual(provider.profile['profile_id'],
                         self.old_att['vectorProfile']['profileId'])
        # The old closure files may now be deleted: only the live rows are read.
        shutil.rmtree(self.old)
        self.validate(record)

    def test_moved_paths_without_closure_move_keep_the_exact_profile(self):
        from laya_provider_reproof import reattest
        new, _closure, socket = self.moved()
        review = self.review(new, self.old_att['dependencyClosure'], socket)
        self.assertEqual(review['reviewedProvider']['vectorProfile'],
                         self.old_att['vectorProfile'])
        self.assertTrue(auto.provider_equivalent(self.old_att, review, self.root))
        self.assertNotIn('profileDependencyClosure',
                         reattest(self.old_att, review['reviewedProvider']))

    def test_changed_content_requires_rebuild(self):
        new, closure, socket = self.moved()
        (new / self.files['model']).write_bytes(b'GGUF other model')
        changed = {'model_sha256': sha_file(new / self.files['model'])}
        for name, review in (
                ('model', self.review(new, closure, socket, **changed)),
                ('node', self.review(new, closure, socket, node_sha256='0' * 64)),
                ('runtime', self.review(new, closure, socket,
                                        runtime_id='qmd-fork-f4633f9-node-llama-cpp-3.20.0')),
                ('recipe', self.review(new, closure, socket,
                                       recipe_id='qmd-query-format-single-document-v1')),
                ('dimension', self.review(new, closure, socket, dimension=1024))):
            with self.subTest(changed=name):
                self.assertFalse(auto.provider_equivalent(self.old_att, review, self.root))
        tampered = self.closure(new / self.native.relative_to(self.old))
        tampered[0] = {**tampered[0], 'sha256': '0' * 64}
        self.assertFalse(auto.provider_equivalent(
            self.old_att, self.review(new, tampered, socket), self.root))

    def test_unpinned_old_runtime_is_never_equivalent_automatically(self):
        from laya_provider_reproof import reattest
        unpinned = self.unpinned()
        self.validate(unpinned)
        new, closure, socket = self.moved()
        review = self.review(new, closure, socket)
        # Same hashed entries, but nothing records the old JS files: rebuild.
        self.assertIsNone(reattest(unpinned, review['reviewedProvider']))
        self.assertFalse(auto.provider_equivalent(unpinned, review, self.root))
        # Pinned to a generation (every real install), the same move is equivalent.
        self.assertTrue(auto.provider_equivalent(self.old_att, review, self.root))

    def test_validator_refuses_a_profile_closure_that_is_not_the_same_files(self):
        from laya_provider_reproof import attestation_record, reattest
        new, closure, socket = self.moved()
        again = reattest(self.old_att, self.review(new, closure, socket)['reviewedProvider'])
        record = attestation_record(proof=self.proof(again['vectorProfile']),
                                    journal_path=self.state / 'install-journal.sqlite',
                                    reattested=again,
                                    schema=bundle.PRODUCTION_ATTESTATION_SCHEMA)
        moved_file = Path(closure[0]['path'])
        moved_file.write_bytes(moved_file.read_bytes()[:-1] + b'X')
        with self.assertRaises(bundle.BundleEvidenceError):
            self.validate(record)
        bad = {**record, 'profileDependencyClosure': record['dependencyClosure']}
        with self.assertRaises(bundle.BundleEvidenceError):
            self.validate(bad)


class WorkerReproof(ReproofBase):
    def run_worker(self, review, attestation=None):
        import laya_active_upgrade
        import laya_product_install as product
        state = self.base / 'worker-state'
        state.mkdir(mode=0o700)
        project = self.base / 'project'
        project.mkdir()
        host = (self.base / 'home' / '.claude' / 'plugins' / 'cache' / 'mkt' /
                'qmd-auto-context' / '0.38.0')
        host.mkdir(parents=True)
        calls = []
        identity = {'pluginRoot': '/old', 'sourceCommit': '1' * 40,
                    'bundleManifestSha256': 'a' * 64,
                    'vectorProfile': self.old_att['vectorProfile']}
        with mock.patch.object(auto, 'installs', return_value=[state]), \
             mock.patch.object(auto, 'classify', return_value=('active', identity)), \
             mock.patch.object(auto, 'installed_commit', return_value=None), \
             mock.patch.object(auto, 'select_source',
                               return_value=(self.base / 'source', '2' * 40)), \
             mock.patch.object(auto, '_not_older'), \
             mock.patch.object(auto, '_read_attestation',
                               return_value=attestation or self.old_att), \
             mock.patch.object(auto, '_ensure_embed_runtime'), \
             mock.patch.object(product, 'review_local_provider', return_value=review), \
             mock.patch.object(product, '_request_build_wake',
                               return_value={'status': 'queued'}), \
             mock.patch.object(laya_active_upgrade, 'upgrade',
                               side_effect=lambda **k: calls.append(k) or
                               {'status': 'active_upgrade_complete'}):
            review['reviewedProvider']['providerManifest'].update(
                providerPath=str(host / 'core/laya_local_embed_provider.py'),
                workerPath=str(host / 'core/laya_local_embed_worker.mjs'))
            results = auto.run(project, root=host, home=self.base / 'home')
        return results, calls, state

    def test_worker_reattests_moved_provider_instead_of_refusing(self):
        new, closure, socket = self.moved()
        results, calls, _state = self.run_worker(self.review(new, closure, socket))
        self.assertEqual(results[0]['status'], 'active_upgrade_complete', results)
        self.assertEqual(len(calls), 1)

    def test_worker_refuses_an_unpinned_runtime_before_copying_it(self):
        new, closure, socket = self.moved()
        results, calls, state = self.run_worker(self.review(new, closure, socket),
                                                attestation=self.unpinned())
        self.assertEqual(results[0], {'stateRoot': str(state), 'status': 'refused',
                                      'reason': 'provider_changed_rebuild_required'})
        self.assertEqual(calls, [])

    def test_worker_refuses_a_changed_model_as_rebuild_required(self):
        new, closure, socket = self.moved()
        (new / self.files['model']).write_bytes(b'GGUF other model')
        review = self.review(new, closure, socket,
                             model_sha256=sha_file(new / self.files['model']))
        results, calls, state = self.run_worker(review)
        self.assertEqual(results[0], {'stateRoot': str(state), 'status': 'refused',
                                      'reason': 'provider_changed_rebuild_required'})
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main(verbosity=1)
