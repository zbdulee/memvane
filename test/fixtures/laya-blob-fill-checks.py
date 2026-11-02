"""Synthetic content fill contracts; no model, Git command or live state.

Claims are cooperative handles, not a hostile same-UID Python boundary. The FD
test models an accidentally closed acquired descriptor, without guessing claim
fields or treating a caller-supplied descriptor as ownership evidence.
"""
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'core'))


def load_fixture(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = load_fixture('blob_fill_helpers', 'laya-blob-store-checks.py')
sets = load_fixture('blob_fill_sets', 'laya-blob-sets-checks.py')


def wait_file(path, seconds=10):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if path.exists():
            return
        time.sleep(0.01)
    raise AssertionError('synthetic child coordination timed out: ' + path.name)


def worker(config_path, mode):
    """Finite synthetic subprocess; inherited claim descriptors are disabled."""
    config = json.loads(Path(config_path).read_text(encoding='utf8'))
    from laya_blob_store import BlobStore, BlobStoreError
    from laya_blob_fill import ContentFiller
    store = BlobStore.open(config['db'], repository=config['repository'], scope_id=config['scope'])
    filler = ContentFiller(store)
    key = (config['blob'], config['recipe'], config['profile'])
    mark = Path(config['mark'])
    release = Path(config['release'])
    fences = []

    def chunker(document, recipe):
        assert not store._db.in_transaction
        return [sets.chunk(document['body'])]

    def embedder(inputs, profile):
        assert not store._db.in_transaction
        fd = os.open(config['calls'], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, b'provider\n')
        finally:
            os.close(fd)
        mark.write_text('provider', encoding='utf8')
        if mode == 'owner':
            wait_file(release)
        return [[1.0, 2.0] for _ in inputs]

    try:
        if mode == 'crash':
            with filler.claim_content(*key):
                mark.write_text('acquired', encoding='utf8')
                wait_file(release)
                os._exit(23)  # Controlled crash releases the OS lease, without TTL logic.
        try:
            result = filler.ensure_content(*key, chunker=chunker, embedder=embedder,
                                           write_fence=lambda: fences.append(1), deadline_seconds=5)
            print(json.dumps({'result': result, 'fences': len(fences)}), flush=True)
        except BlobStoreError as exc:
            print(json.dumps({'error': exc.reason, 'fences': len(fences)}), flush=True)
    finally:
        store.close()


class ContentFillChecks(helpers.BlobStoreCase):
    def setUp(self):
        super().setUp()
        try:
            module = importlib.import_module('laya_blob_fill')
        except ModuleNotFoundError as exc:
            if exc.name != 'laya_blob_fill':
                raise
            self.fail('PR4 ContentFiller API is absent: core/laya_blob_fill.py')
        self.Filler = getattr(module, 'ContentFiller', None)
        self.assertTrue(callable(self.Filler), 'PR4 ContentFiller API is absent')
        for method in ('claim_content', 'validate_claim', 'ensure_content'):
            self.assertTrue(callable(getattr(self.Filler, method, None)), 'missing ContentFiller.' + method)
        self.assertTrue(callable(getattr(self.Store, 'embedding_inputs', None)),
                        'PR4 BlobStore.embedding_inputs API is absent')
        self.store, self.repository, self.wid = self.create()
        self.blob = self.store.ingest_card(self.wid, 'cards/a.md')
        self.recipe = self.store.register_recipe(sets.recipe())
        self.profile = self.store.register_profile(sets.profile())
        self.filler = self.Filler(self.store)
        self.key = (self.blob, self.recipe, self.profile)
        self.lock_root = self.db.with_name(self.db.name + '.content-locks')
        self.fences, self.documents, self.inputs = [], [], []

    def chunker(self, document, recipe):
        self.assertFalse(self.store._db.in_transaction, 'chunker runs outside SQL transaction')
        self.assertEqual(recipe, sets.recipe())
        self.documents.append(dict(document))
        return [sets.chunk(document['body'])]

    def embedder(self, inputs, profile):
        self.assertFalse(self.store._db.in_transaction, 'embedder runs outside SQL transaction')
        self.assertEqual(profile, sets.profile())
        self.inputs.append(list(inputs))
        return [[1.0, 2.0] for _ in inputs]

    def fill(self, *, filler=None, key=None, **kwargs):
        options = dict(chunker=self.chunker, embedder=self.embedder,
                       write_fence=lambda: self.fences.append(1))
        options.update(kwargs)
        return (filler or self.filler).ensure_content(*(key or self.key), **options)

    def preload(self):
        self.store.put_chunk_set(self.blob, self.recipe, [sets.chunk(helpers.CARD.decode('utf8'))])
        expected = 'title: Shared Title | text: ' + helpers.CARD.decode('utf8')
        return self.store.put_embedding_set(self.blob, self.recipe, self.profile,
            [dict(seq=0, input_sha=hashlib.sha256(expected.encode('utf8')).hexdigest(),
                  embedding=struct.pack('<ff', 1, 2))])

    def no_embedding(self):
        counts = self.store.counts()
        self.assertEqual((counts['embedding_sets'], counts['vectors']), (0, 0))
        self.assertFalse(self.store._db.in_transaction)

    def lock_file(self):
        files = list(self.lock_root.iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].is_file())
        self.assertRegex(files[0].name, r'^[0-9a-f]{64}(?:\.lock)?$')
        return files[0]

    def mutate_claim(self, mutation):
        manager = self.filler.claim_content(*self.key)
        claim = manager.__enter__()
        try:
            mutation(claim)
            self.refused(self.filler.validate_claim, claim)
        finally:
            with contextlib.suppress(self.Error, OSError):
                manager.__exit__(None, None, None)

    def child_config(self):
        config = dict(db=str(self.db), repository=self.repository, scope=self.scope,
                      blob=self.blob, recipe=self.recipe, profile=self.profile,
                      mark=str(self.home / 'child-mark'), release=str(self.home / 'child-release'),
                      calls=str(self.home / 'provider-calls'))
        path = self.home / 'child-config.json'
        path.write_text(json.dumps(config), encoding='utf8')
        return path, config

    def start_child(self, path, mode):
        child = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()),
                                  '--worker', str(path), mode],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, encoding='utf8', close_fds=True,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                      'QMD_SETUP_GUARD_FIXTURE': '1'})
        def cleanup():
            if child.poll() is None:
                child.terminate()
                try:
                    child.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=3)
            else:
                child.communicate(timeout=3)
        self.addCleanup(cleanup)
        return child

    def finish_child(self, child, expected=0):
        stdout, stderr = child.communicate(timeout=15)
        self.assertEqual(child.returncode, expected, stderr)
        self.assertEqual(stderr, '')
        return json.loads(stdout) if stdout else None

    def test_01_fill_publishes_complete_vectors_and_exact_inputs(self):
        result = self.fill()
        self.assertEqual(result['status'], 'filled')
        self.assertEqual((result['blob_sha'], result['recipe_id'], result['profile_id']), self.key)
        self.assertEqual(result['chunk_count'], 1)
        self.assertRegex(result['set_digest'], r'^[0-9a-f]{64}$')
        self.assertEqual(self.documents, [dict(body=helpers.CARD.decode('utf8'),
            title='Shared Title', path=f'blobs/{self.blob}.md')])
        expected = ['title: Shared Title | text: ' + helpers.CARD.decode('utf8')]
        self.assertEqual(self.inputs, [expected])
        self.assertEqual(self.store.embedding_inputs(*self.key), expected)
        rows = self.store.vectors(*self.key)
        self.assertEqual(rows, [dict(seq=0, input_sha=hashlib.sha256(expected[0].encode('utf8')).hexdigest(),
                                    embedding=struct.pack('<ff', 1, 2))])
        self.assertEqual(self.store.vector_input_sha(*self.key, 0), rows[0]['input_sha'])
        self.assertEqual(self.fences, [1])
        self.assertTrue(self.store.verify())

    def test_02_complete_cache_hit_has_no_write_fence_callback_or_lock_directory(self):
        digest = self.preload()
        before, changes, sql = self.store.counts(), self.store._db.total_changes, []
        self.assertFalse(self.lock_root.exists())
        self.store._db.set_trace_callback(sql.append)
        writes = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                  sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_TRIGGER}
        self.store._db.set_authorizer(lambda action, *_: sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK)
        try:
            result = self.fill(write_fence=lambda: self.fail('cache hit called fence'))
        finally:
            self.store._db.set_authorizer(None)
            self.store._db.set_trace_callback(None)
        self.assertEqual((result['status'], result['set_digest']), ('cache_hit', digest))
        self.assertEqual((self.documents, self.inputs, self.fences), ([], [], []))
        self.assertEqual((self.store.counts(), self.store._db.total_changes), (before, changes))
        self.assertFalse(any(statement.upper().startswith(('BEGIN IMMEDIATE', 'BEGIN EXCLUSIVE')) for statement in sql))
        self.assertFalse(self.lock_root.exists())

    def test_03_titleless_rename_reuses_synthetic_path_and_provider_input(self):
        body = '별빛😀\r\n'
        for name in ('first.md', 'renamed.md'):
            (self.wiki() / name).write_bytes(body.encode('utf8'))
        first = self.store.ingest_card(self.wid, 'first.md')
        second = self.store.ingest_card(self.wid, 'renamed.md')
        self.assertEqual(first, second)
        key = (first, self.recipe, self.profile)
        one = self.fill(key=key)
        two = self.fill(key=(second, self.recipe, self.profile))
        self.assertEqual((one['status'], two['status']), ('filled', 'cache_hit'))
        self.assertEqual(self.documents, [dict(body=body, title=first, path=f'blobs/{first}.md')])
        self.assertEqual(self.inputs, [[f'title: {first} | text: {body}']])
        self.assertEqual(one['set_digest'], two['set_digest'])

    def test_04_recipe_change_misses_without_overwriting_prior_set(self):
        first = self.fill()
        spec = sets.recipe(); spec['window_tokens'] = 901
        changed = self.store.register_recipe(spec)
        second = self.fill(key=(self.blob, changed, self.profile),
                           chunker=lambda document, recipe: [sets.chunk(document['body'])])
        self.assertEqual(second['status'], 'filled')
        self.assertNotEqual(first['set_digest'], second['set_digest'])
        self.assertEqual((len(self.inputs), self.store.counts()['embedding_sets']), (2, 2))
        self.assertEqual(self.fill()['set_digest'], first['set_digest'])

    def test_05_profile_change_reembeds_only_changed_key(self):
        first = self.fill()
        spec = sets.profile(); spec['model_sha'] = '8' * 64
        changed = self.store.register_profile(spec)
        observed = []
        def embed(inputs, profile):
            observed.append(profile)
            return [[2, 3] for _ in inputs]
        second = self.fill(key=(self.blob, self.recipe, changed), embedder=embed)
        self.assertEqual((second['status'], observed), ('filled', [spec]))
        self.assertNotEqual(first['set_digest'], second['set_digest'])
        self.assertEqual(self.fill()['set_digest'], first['set_digest'])

    def test_06_chunker_failure_does_not_publish_and_is_retryable(self):
        def fail(*_):
            raise RuntimeError('synthetic chunker failure')
        with self.assertRaises((RuntimeError, self.Error)):
            self.fill(chunker=fail)
        self.no_embedding()
        self.assertEqual(self.fill()['status'], 'filled')

    def test_07_provider_failure_does_not_publish_and_is_retryable(self):
        def fail(*_):
            raise RuntimeError('synthetic provider failure')
        with self.assertRaises((RuntimeError, self.Error)):
            self.fill(embedder=fail)
        self.no_embedding()
        self.assertEqual(self.fill()['status'], 'filled')

    def test_08_wrong_vector_count_never_publishes_partial_set(self):
        self.refused(self.fill, embedder=lambda *_: [])
        self.no_embedding()

    def test_09_wrong_dimension_is_refused(self):
        self.refused(self.fill, embedder=lambda *_: [[1]])
        self.no_embedding()

    def test_10_nan_is_refused(self):
        self.refused(self.fill, embedder=lambda *_: [[float('nan'), 1]])
        self.no_embedding()

    def test_11_zero_vector_is_refused(self):
        self.refused(self.fill, embedder=lambda *_: [[0, -0.0]])
        self.no_embedding()

    def test_12_float32_overflow_is_refused(self):
        self.refused(self.fill, embedder=lambda *_: [[1e100, 1]])
        self.no_embedding()

    def test_13_embedding_child_failure_rolls_back_parent_and_children(self):
        def authorizer(action, table, *_):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_INSERT and table == 'vectors' else sqlite3.SQLITE_OK
        self.store._db.set_authorizer(authorizer)
        try:
            self.refused(self.fill)
        finally:
            self.store._db.set_authorizer(None)
        self.no_embedding()
        self.assertEqual(self.fill()['status'], 'filled')

    def test_14_chunk_output_above_bound_never_calls_provider(self):
        (self.wiki() / 'many.md').write_text('x' * 1025, encoding='utf8')
        blob = self.store.ingest_card(self.wid, 'many.md')
        self.refused(self.fill, key=(blob, self.recipe, self.profile),
                     chunker=lambda *_: [sets.chunk('x', seq=i, position=i) for i in range(1025)])
        self.assertEqual(self.inputs, [])
        self.no_embedding()

    def test_15_fence_precedes_first_mutation_and_refusal_writes_nothing(self):
        before, changes = self.store.counts(), self.store._db.total_changes
        def fence():
            self.assertFalse(self.store._db.in_transaction)
            self.assertEqual((self.store.counts(), self.store._db.total_changes), (before, changes))
            raise RuntimeError('synthetic fence refusal')
        with self.assertRaises((RuntimeError, self.Error)):
            self.fill(write_fence=fence)
        self.assertEqual((self.store.counts(), self.store._db.total_changes), (before, changes))
        self.no_embedding()

    def test_16_deadline_requires_finite_positive_bounded_seconds(self):
        for seconds in (0, -1, True, float('nan'), float('inf'), 301, 10**1000):
            with self.subTest(seconds=seconds):
                self.refused(self.fill, deadline_seconds=seconds)
        self.assertEqual((self.inputs, self.documents, self.fences), ([], [], []))
        self.no_embedding()

    def test_17_forged_claim_is_refused(self):
        for claim in (None, object(), {'pid': os.getpid(), 'ttl': 999999, 'fd': 0}):
            with self.subTest(kind=type(claim).__name__):
                self.refused(self.filler.validate_claim, claim)

    def test_18_other_filler_and_other_store_cannot_use_claim(self):
        other = self.Filler(self.store)
        reopened = self.Filler(self.reopen(self.repository))
        with self.filler.claim_content(*self.key) as claim:
            self.filler.validate_claim(claim)
            self.refused(other.validate_claim, claim)
            self.refused(reopened.validate_claim, claim)

    def test_19_released_claim_is_stale_and_lock_inode_is_retained(self):
        with self.filler.claim_content(*self.key) as claim:
            lock = self.lock_file(); node = lock.stat()
        self.refused(self.filler.validate_claim, claim)
        with self.filler.claim_content(*self.key) as replacement:
            self.filler.validate_claim(replacement)
        self.assertEqual((lock.stat().st_dev, lock.stat().st_ino), (node.st_dev, node.st_ino))

    def test_20_closed_fd_reused_for_other_inode_is_refused_and_not_closed_by_cleanup(self):
        foreign = self.home / 'unrelated-file'
        foreign.write_text('keep open', encoding='utf8')
        retained = []
        def close_fd(claim):
            node = self.lock_file().stat()
            matches = []
            for name in os.listdir('/dev/fd'):
                try:
                    info = os.fstat(int(name))
                except (OSError, ValueError):
                    continue
                if (info.st_dev, info.st_ino) == (node.st_dev, node.st_ino):
                    matches.append(int(name))
            self.assertEqual(len(matches), 1, 'one internal acquired lock FD')
            self.assertFalse(os.get_inheritable(matches[0]))
            acquired = matches[0]
            os.close(acquired)
            replacement = os.open(foreign, os.O_RDONLY | os.O_CLOEXEC)
            if replacement != acquired:
                os.dup2(replacement, acquired, inheritable=False)
                os.close(replacement)
            retained.append(acquired)
            self.assertNotEqual(os.fstat(acquired).st_ino, node.st_ino)
        try:
            self.mutate_claim(close_fd)
            self.assertEqual(len(retained), 1)
            info = os.fstat(retained[0])
            self.assertEqual((info.st_dev, info.st_ino), (foreign.stat().st_dev, foreign.stat().st_ino),
                             'claim cleanup must not close a reused descriptor for another inode')
        finally:
            for fd in retained:
                with contextlib.suppress(OSError):
                    os.close(fd)

    def test_21_replaced_lock_inode_invalidates_claim(self):
        def replace(_):
            lock = self.lock_file()
            lock.rename(lock.with_name(lock.name + '.old'))
            lock.touch(mode=0o600)
        self.mutate_claim(replace)

    def test_22_replaced_lock_root_invalidates_claim(self):
        def replace(_):
            self.lock_root.rename(self.lock_root.with_name(self.lock_root.name + '.old'))
            self.lock_root.mkdir(mode=0o700)
        self.mutate_claim(replace)

    def test_23_wrong_lock_and_root_modes_are_refused(self):
        with self.filler.claim_content(*self.key):
            lock = self.lock_file()
        self.assertEqual((stat.S_IMODE(self.lock_root.stat().st_mode), stat.S_IMODE(lock.stat().st_mode)),
                         (0o700, 0o600))
        for path, bad, good in ((lock, 0o644, 0o600), (self.lock_root, 0o755, 0o700)):
            path.chmod(bad)
            try:
                self.refused(self.fill)
            finally:
                path.chmod(good)
        self.assertEqual((self.inputs, self.fences), ([], []))

    def test_24_symlink_lock_is_refused_without_following_target(self):
        with self.filler.claim_content(*self.key):
            lock = self.lock_file()
        target = lock.with_name(lock.name + '.target')
        before = lock.read_bytes()
        lock.rename(target)
        lock.symlink_to(target.name)
        self.refused(self.fill)
        self.assertEqual((self.inputs, self.fences), ([], []))
        self.assertEqual(target.read_bytes(), before)

    def test_25_hardlinked_lock_is_refused(self):
        with self.filler.claim_content(*self.key):
            lock = self.lock_file()
        os.link(lock, self.home / 'extra-link')
        self.refused(self.fill)
        self.assertEqual((self.inputs, self.fences), ([], []))

    def test_26_two_processes_coalesce_provider_and_busy_path_has_no_writes(self):
        path, config = self.child_config()
        owner = self.start_child(path, 'owner')
        wait_file(Path(config['mark']))
        before, changes = self.store.counts(), self.store._db.total_changes
        loser = self.finish_child(self.start_child(path, 'contender'))
        self.assertTrue(loser.get('error'))
        self.assertEqual(loser['fences'], 0)
        self.assertEqual((self.store.counts(), self.store._db.total_changes), (before, changes))
        self.assertFalse(self.store._db.in_transaction)
        Path(config['release']).write_text('release', encoding='utf8')
        first = self.finish_child(owner)
        second = self.finish_child(self.start_child(path, 'contender'))
        self.assertEqual((first['result']['status'], second['result']['status']), ('filled', 'cache_hit'))
        self.assertEqual((first['fences'], second['fences']), (1, 0))
        self.assertEqual(first['result']['set_digest'], second['result']['set_digest'])
        self.assertEqual(Path(config['calls']).read_text(encoding='utf8').splitlines(), ['provider'])

    def test_27_crash_releases_os_lock_without_ttl_or_lock_unlink(self):
        path, config = self.child_config()
        owner = self.start_child(path, 'crash')
        wait_file(Path(config['mark']))
        lock = self.lock_file(); before = lock.stat()
        busy = self.finish_child(self.start_child(path, 'contender'))
        self.assertTrue(busy.get('error'))
        self.assertEqual(busy['fences'], 0)
        Path(config['release']).write_text('crash now', encoding='utf8')
        self.finish_child(owner, expected=23)
        recovered = self.finish_child(self.start_child(path, 'contender'))
        self.assertEqual(recovered['result']['status'], 'filled')
        self.assertEqual((lock.stat().st_dev, lock.stat().st_ino), (before.st_dev, before.st_ino))
        self.assertEqual(Path(config['calls']).read_text(encoding='utf8').splitlines(), ['provider'])

    def test_28_symlink_lock_root_is_refused_without_following_target(self):
        with self.filler.claim_content(*self.key):
            lock = self.lock_file()
        old = self.lock_root.with_name(self.lock_root.name + '.target')
        before = lock.read_bytes()
        self.lock_root.rename(old)
        self.lock_root.symlink_to(old.name, target_is_directory=True)
        self.refused(self.fill)
        self.assertEqual((self.inputs, self.fences), ([], []))
        self.assertEqual((old / lock.name).read_bytes(), before)

    def test_29_closed_store_cannot_validate_live_claim(self):
        self.mutate_claim(lambda _: self.store.close())

    def test_30_expired_callback_result_cannot_publish_embedding_set(self):
        def slow(inputs, profile):
            self.assertFalse(self.store._db.in_transaction)
            time.sleep(0.04)
            return [[1, 2] for _ in inputs]
        self.refused(self.fill, embedder=slow, deadline_seconds=0.02)
        self.no_embedding()

    def test_31_cache_validation_cannot_return_success_after_deadline(self):
        self.preload()
        before = self.store._db.total_changes
        cached = self.filler._cached
        clock = [0.0]
        def expires(key):
            result = cached(key)
            clock[0] = 2.0
            return result
        with mock.patch('laya_blob_fill.time.monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(self.filler, '_cached', side_effect=expires):
            self.refused(self.fill, deadline_seconds=1)
        self.assertEqual(self.store._db.total_changes, before)
        self.assertEqual((self.fences, self.documents, self.inputs), ([], [], []))


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--worker':
        worker(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
