#!/usr/bin/env python3
"""Disposable development-install rehearsal; never a formal L1 host QA pass.

All Git commits, QMD-format databases, package roots, journal and project files
are created under one temporary directory. No installed QMD or user settings
are read. The formal release gate still requires marketplace installation and
fresh Claude/Codex sessions on the exact final artifact.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest

REPO = Path(__file__).resolve().parents[2]
CORE = REPO / 'core'
sys.path.insert(0, str(CORE))
MODEL_ID = 'laya-l0-precomputed-fixture'
EMBEDDING_FINGERPRINT = 'c' * 64
PROVIDER_RELATIVE = 'core/laya_l0_fixture_provider.py'
PROVIDER_SOURCE = '''"""Disposable precomputed vector fixture; not a production model provider."""
import hashlib

MODEL_ID = "laya-l0-precomputed-fixture"
EMBEDDING_FINGERPRINT = "''' + EMBEDDING_FINGERPRINT + '''"
DIMENSION = 2
DOCUMENT_VECTORS = {
    hashlib.sha256(b"hello upgrade\\n").hexdigest(): (1.0, 0.0),
    hashlib.sha256(b"emerald heron after update\\n").hexdigest(): (0.0, 1.0),
}
QUERY_VECTORS = {
    "hello": (1.0, 0.0),
    "emerald heron": (0.0, 1.0),
}

def document_vector(content_hash):
    return DOCUMENT_VECTORS[content_hash]

def query_vector(prompt):
    return QUERY_VECTORS[prompt]
'''


def _vector_profile():
    identity = [MODEL_ID, EMBEDDING_FINGERPRINT, 2, 'cosine']
    canonical = json.dumps(identity, sort_keys=True, ensure_ascii=False,
                           separators=(',', ':')).encode()
    return {'profileId': hashlib.sha256(canonical).hexdigest(),
            'modelId': MODEL_ID, 'embeddingFingerprint': EMBEDDING_FINGERPRINT,
            'dimension': 2, 'metric': 'cosine',
            'approvedProviderId': 'laya-l0-precomputed-fixture',
            'providerArtifactSha256': hashlib.sha256(PROVIDER_SOURCE.encode()).hexdigest(),
            'providerRelativePath': PROVIDER_RELATIVE}


DEVELOPMENT_VECTOR_CHILD = r'''
import hashlib, importlib.util, json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from laya_index_a import IndexContractError
from laya_index_host_route import select_route
from laya_install_journal import (cas_mark_new_write, inspect_journal,
    verify_development_vector_approval)
from laya_index_runtime import LayaIndexRuntime
from laya_index_source_port import SourceStale, scan
from laya_index_sync import sync_full
from laya_index_vector import (qmd_markdown_title, search_vector_verified)
from laya_host_response import HostResponseError, query_selected_vector, render_context

decision = select_route(sys.argv[2])
approval_path = sys.argv[3]
proof = verify_development_vector_approval(decision, approval_path)
assert proof['developmentOnlyRouteAllowed'] and not proof['productRouteAllowed']
profile = proof['vectorProfile']
provider_path = Path(decision['pluginRoot']) / profile['providerRelativePath']
assert hashlib.sha256(provider_path.read_bytes()).hexdigest() == profile['providerArtifactSha256']
spec = importlib.util.spec_from_file_location('laya_l0_fixture_provider', provider_path)
provider = importlib.util.module_from_spec(spec)
spec.loader.exec_module(provider)
assert (provider.MODEL_ID, provider.EMBEDDING_FINGERPRINT, provider.DIMENSION) == (
    profile['modelId'], profile['embeddingFingerprint'], profile['dimension'])

with LayaIndexRuntime(source_root=decision['sourceRoot'], scope=decision['scopeId'],
        db_path=decision['dbPath'], queue_path=decision['queuePath'],
        db_incarnation=decision['dbIncarnation']) as runtime:
    view = scan(runtime.source_root, runtime.scope, runtime.index)
    chunks = [{'hash': digest, 'title': title, 'seq': 0, 'pos': 0,
               'vector': list(provider.document_vector(digest))}
              for digest, title in sorted({(f.sha256, qmd_markdown_title(f.text, f.path))
                                           for f in view.files if f.eligible})]
    batch = {'scope': runtime.scope, 'source_manifest': view.manifest,
             'model_id': profile['modelId'],
             'embedding_fingerprint': profile['embeddingFingerprint'],
             'dimension': profile['dimension'], 'chunks': chunks}
    journal_before = inspect_journal(decision['journalPath'])
    unavailable = sync_full(runtime, before_publish=lambda: 1 / 0)
    assert unavailable == {'state': 'deferred', 'generation': None, 'claim': None,
                           'reason': 'vector_provider_unavailable'}
    assert inspect_journal(decision['journalPath']) == journal_before
    old_token, old_gen = runtime.index.pin(runtime.scope)
    state = [proof]
    def before_publish():
        marked = cas_mark_new_write(decision['journalPath'], state[0]['selectionEpoch'])
        state[0] = verify_development_vector_approval(decision, approval_path)
        assert marked['newWriteSeen'] and state[0]['selectionEpoch'] == marked['epoch']
    changed = sync_full(runtime, before_publish=before_publish, vector_batch=batch)
    assert changed['state'] == 'indexed'
    assert changed['generation'] != old_gen
    assert inspect_journal(decision['journalPath'])['newWriteSeen']
    try:
        search_vector_verified(runtime.index, runtime.source_root, old_token,
            [1.0, 0.0], model_id=profile['modelId'],
            embedding_fingerprint=profile['embeddingFingerprint'],
            dimension=profile['dimension'], limit=5)
    except SourceStale as exc:
        assert 'source manifest differs from view seal' in str(exc)
    else:
        raise AssertionError('old generation returned stale source')
    finally:
        runtime.index.release(old_token)
    def approved_query(prompt, active_profile):
        return {'profileId': active_profile['profileId'],
                'approvedProviderId': active_profile['approvedProviderId'],
                'providerArtifactSha256': active_profile['providerArtifactSha256'],
                'vector': list(provider.query_vector(prompt))}
    rows = query_selected_vector(runtime, 'emerald heron', profile,
                                 state[0]['vectorProfile'], approved_query)
    assert [r['path'] for r in rows] == ['live.md']
    assert [r['body'] for r in rows] == ['emerald heron after update\n']
    context = render_context(decision, rows)
    assert 'foreign emerald secret' not in context and 'hello upgrade' not in context
    old_query = query_selected_vector(runtime, 'hello', profile,
                                      state[0]['vectorProfile'], approved_query)
    assert [r['body'] for r in old_query] == ['emerald heron after update\n']
    try:
        query_selected_vector(runtime, 'emerald heron',
            {**profile, 'dimension': 3}, state[0]['vectorProfile'], approved_query)
    except HostResponseError as exc:
        assert 'vector_provider_missing' in str(exc)
    else:
        raise AssertionError('dimension mismatch accepted')
    try:
        query_selected_vector(runtime, 'emerald heron', profile,
            state[0]['vectorProfile'], lambda _prompt, _profile: {
                **approved_query('emerald heron', profile),
                'providerArtifactSha256': '0' * 64})
    except HostResponseError as exc:
        assert 'vector_provider_mismatch' in str(exc)
    else:
        raise AssertionError('provider artifact mismatch accepted')
    repeated = sync_full(runtime, before_publish=lambda: 1 / 0,
                         vector_batch=batch)
    assert repeated['state'] == 'unchanged'
    assert inspect_journal(decision['journalPath'])['epoch'] == state[0]['selectionEpoch']
    print(json.dumps({'development': True, 'changed': changed['state'],
                      'repeated': repeated['state'], 'paths': [r['path'] for r in rows],
                      'newWriteSeen': True}))
'''

from laya_index_a import LayaIndexA
from laya_index_claim_queue import ClaimQueue
from laya_index_source_port import scan
from laya_index_vector import (LayaVectorIndex, qmd_markdown_title,
                               source_evidence)
from laya_index_host_route import FIELDS, RECORD, SCHEMA, select_route
from laya_install_journal import (JournalEvidenceError, cas_activate,
    cas_mark_new_write, cas_recover, cas_restore, cas_stage_update,
    create_journal, inspect_journal, verify_selected_route)
from laya_product_bundle import (BundleEvidenceError, PLUGIN_PREFIXES, PLUGIN_ROOT_FILES,
    inspect_full_plugin_candidate, prepare_full_plugin_candidate)
from laya_qmd_migration import (QMD_253, backup_qmd, inspect_qmd,
    prepare_qmd_candidate, verify_restore)


def _run_git(root, *args):
    env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_SYSTEM': os.devnull}
    result = subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                             '-c', 'commit.gpgsign=false', *args], check=True,
                            capture_output=True, text=True, timeout=30, env=env)
    return result.stdout.strip()


def _copy_plugin_sources(source):
    """Copy only source-controlled plugin code into a disposable Git repository."""
    names = subprocess.run(
        ['git', '-C', str(REPO), 'ls-files', '--cached', '--others',
         '--exclude-standard', '--', *[p[:-1] for p in PLUGIN_PREFIXES],
         *sorted(PLUGIN_ROOT_FILES)], check=True, capture_output=True,
        text=True, timeout=30).stdout.splitlines()
    for name in names:
        origin = REPO / name
        if not origin.is_file() or origin.is_symlink():
            continue
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, target)
    provider = source / PROVIDER_RELATIVE
    provider.parent.mkdir(parents=True, exist_ok=True)
    provider.write_text(PROVIDER_SOURCE, encoding='utf-8')
    _run_git(source, 'add', '--', '.')
    _run_git(source, '-c', 'user.name=Fixture', '-c',
             'user.email=fixture@example.invalid', 'commit', '-q',
             '-m', 'synthetic fixture candidate v1')
    return _run_git(source, 'rev-parse', 'HEAD')


class DevelopmentInstallRehearsal(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='laya-l1-dev-rehearsal-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.previous_home = os.environ.get('HOME')
        os.environ['HOME'] = str(self.root)
        self.addCleanup(self._restore_home)
        self.global_qmd = self.root / 'separate-global-qmd'
        self.global_qmd.mkdir()
        self.global_sentinel = self.global_qmd / 'do-not-change'
        self.global_sentinel.write_bytes(b'user-owned-global-qmd\n')
        self.global_before = self.global_sentinel.stat()
        self.project = self.root / 'project'
        self.docs = self.project / 'docs'
        self.docs.mkdir(parents=True)
        self.auto = self.project / '.auto-context'
        self.auto.mkdir()
        (self.docs / 'live.md').write_text('hello upgrade\n', encoding='utf-8')
        self.settings = self.auto / 'settings.json'
        self.settings.write_text(json.dumps({'collections': ['docs'], 'indexing': True,
                                             'recallStrategy': 'flat',
                                             'collectionPaths': {'docs': 'docs'},
                                             'compile': {'mode': 'off'}}),
                                 encoding='utf-8')

    def _restore_home(self):
        if self.previous_home is None:
            os.environ.pop('HOME', None)
        else:
            os.environ['HOME'] = self.previous_home

    def _global_untouched(self):
        now = self.global_sentinel.stat()
        self.assertEqual(self.global_sentinel.read_bytes(), b'user-owned-global-qmd\n')
        self.assertEqual((now.st_dev, now.st_ino, now.st_mtime_ns, now.st_size),
                         (self.global_before.st_dev, self.global_before.st_ino,
                          self.global_before.st_mtime_ns, self.global_before.st_size))

    def _flip_entrypoint(self, plugin_root):
        """Alter only a copied host entrypoint, retaining its executable mode."""
        entry = Path(plugin_root) / 'hooks' / 'run-hook'
        original = entry.read_bytes()
        self.assertTrue(original)
        mode = stat.S_IMODE(entry.stat().st_mode)
        entry.chmod(mode | stat.S_IWUSR)
        entry.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        entry.chmod(mode)

    def _alter_core(self, plugin_root):
        entry = Path(plugin_root) / 'core' / 'laya_index_a.py'
        mode = stat.S_IMODE(entry.stat().st_mode)
        entry.chmod(mode | stat.S_IWUSR)
        with entry.open('ab') as output:
            output.write(b'\n# test-owned post-install byte alteration\n')
        entry.chmod(mode)

    def _route(self, proof, generation, install_id, journal, db, queue, incarnation):
        route = {'schema': SCHEMA, 'status': 'active',
                 'projectRoot': str(self.project),
                 'settingsSha256': hashlib.sha256(self.settings.read_bytes()).hexdigest(),
                 'dbPath': str(db), 'queuePath': str(queue),
                 'dbIncarnation': incarnation, 'scopeId': 'a' * 32,
                 'collectionName': 'docs', 'sourceRoot': str(self.docs),
                 'pluginRoot': proof['pluginRoot'], 'journalPath': str(journal),
                 'installId': install_id, 'generation': generation,
                 'bundleManifestSha256': proof['bundleManifestSha256'],
                 'ownershipManifestSha256': proof['ownershipManifestSha256'],
                 'vectorProfile': proof['vectorProfile']}
        self.assertEqual(set(route), FIELDS)
        return json.dumps(route, sort_keys=True, separators=(',', ':')).encode()

    def _source_revision(self):
        source = self.root / 'synthetic-git-source'
        source.mkdir()
        _run_git(source, 'init', '-q')
        first = _copy_plugin_sources(source)
        # A comment changes executable artifact identity without changing the
        # synthetic query's behavior. This is not an actual prior release.
        with (source / 'core' / 'laya_index_a.py').open('a', encoding='utf-8') as out:
            out.write('\n# disposable second candidate revision\n')
        _run_git(source, 'add', '--', 'core/laya_index_a.py')
        _run_git(source, '-c', 'user.name=Fixture', '-c',
                 'user.email=fixture@example.invalid', 'commit', '-q',
                 '-m', 'synthetic fixture candidate v2')
        second = _run_git(source, 'rev-parse', 'HEAD')
        return source, first, second

    def test_two_candidate_update_restart_and_rollback_fences(self):
        source, first, second = self._source_revision()
        install = self.root / 'private-install'
        install.mkdir(mode=0o700)
        # The source tree is at v2. A detached disposable worktree supplies
        # exact v1 bytes without changing this checkout or an installed copy.
        prior = self.root / 'synthetic-prior-source'
        _run_git(source, 'worktree', 'add', '--detach', str(prior), first)
        profile = _vector_profile()
        one = prepare_full_plugin_candidate(prior, install / 'plugin-v1',
                                            commit_id=first, vector_profile=profile)
        two = prepare_full_plugin_candidate(source, install / 'plugin-v2',
                                            commit_id=second, vector_profile=profile)
        self.assertNotEqual(one['sourceCommit'], two['sourceCommit'])
        self.assertEqual(inspect_full_plugin_candidate(one['pluginRoot']), one)
        self.assertEqual(inspect_full_plugin_candidate(two['pluginRoot']), two)

        db, queue = install / 'index.sqlite', install / 'claims.sqlite'
        with closing(LayaIndexA(db, create=True)) as index:
            incarnation = index.incarnation()
            source_view = scan(str(self.docs), 'a' * 32, index)
            self.assertEqual(index.stage('seed', 'a' * 32, source_view.documents(),
                                         source_manifest=source_view.manifest,
                                         claim_digest='b' * 64, claim_epoch=0),
                             'prepared')
            vectors = LayaVectorIndex(index)
            profile_id = vectors.register_profile(MODEL_ID,
                                                  EMBEDDING_FINGERPRINT, 2)
            self.assertEqual(profile_id, profile['profileId'])
            for item in source_view.files:
                if item.eligible:
                    title = qmd_markdown_title(item.text, item.path)
                    vectors.insert_chunk(item.sha256, profile_id, 0, 0,
                                         [1.0, 0.0], source_evidence(item.sha256, title),
                                         title=title)
                    vectors.seal_chunk_set(item.sha256, title, profile_id, 1)
            self.assertEqual(vectors.seal_generation('seed', profile_id)
                             ['profile_id'], profile_id)
            self.assertEqual(index.publish(None, 'seed', 'seed-claim',
                                           source_manifest=source_view.manifest,
                                           claim_digest='b' * 64, claim_epoch=0,
                                           require_vector=True),
                             'published')
            # The same DB also holds a second scope with a matching query
            # vector. A selected scope must never return this membership.
            foreign_body = 'foreign emerald secret\n'
            foreign_hash = hashlib.sha256(foreign_body.encode()).hexdigest()
            self.assertEqual(index.stage('foreign-seed', 'e' * 32,
                                         [('foreign.md', foreign_body, True)]),
                             'prepared')
            foreign_title = qmd_markdown_title(foreign_body, 'foreign.md')
            vectors.insert_chunk(foreign_hash, profile_id, 0, 0,
                                 [0.0, 1.0], source_evidence(foreign_hash, foreign_title),
                                 title=foreign_title)
            vectors.seal_chunk_set(foreign_hash, foreign_title, profile_id, 1)
            vectors.seal_generation('foreign-seed', profile_id)
            self.assertEqual(index.publish(None, 'foreign-seed', 'foreign-claim',
                                           require_vector=True), 'published')
        with closing(ClaimQueue.create(queue, index_incarnation=incarnation)):
            pass
        journal = install / 'install-journal.sqlite'
        old = self._route(one, 'g-' + '1' * 32, '1' * 32, journal,
                          db, queue, incarnation)
        new = self._route(two, 'g-' + '2' * 32, '2' * 32, journal,
                          db, queue, incarnation)
        route_path = self.auto / RECORD
        initial = create_journal(journal, one, old, user_qmd_roots=[self.global_qmd])
        self.assertEqual(initial['state'], 'candidate_ready')
        with self.assertRaises(JournalEvidenceError):
            cas_activate(journal, initial['epoch'], old)
        route_path.write_bytes(old)
        active_old = cas_activate(journal, initial['epoch'], old)
        self.assertEqual(active_old['selected']['sourceCommit'], first)
        self.assertEqual(select_route(str(self.project))['state'], 'candidate')

        pending = cas_stage_update(journal, active_old['epoch'], two, new)
        self.assertEqual(pending['state'], 'pending')
        self.assertEqual(pending['selected']['sourceCommit'], first)
        # Restart with old route still on disk: exact old selection wins.
        retained = cas_recover(journal, pending['epoch'], old)
        self.assertEqual(retained['state'], 'restored_old')
        self.assertEqual(retained['selected']['sourceCommit'], first)
        pending = cas_stage_update(journal, retained['epoch'], two, new)
        with self.assertRaisesRegex(JournalEvidenceError, 'journal_not_active'):
            cas_mark_new_write(journal, pending['epoch'])
        # An owned child publishes only the disposable route file and exits
        # abnormally before journal activation. Normal OS restart/recovery is
        # exercised here; this is not a power-loss durability test.
        interrupted = subprocess.run([sys.executable, '-c',
            'import os,sys; p=sys.argv[1]; data=bytes.fromhex(sys.argv[2]); '
            'f=os.open(p,os.O_WRONLY|os.O_TRUNC|os.O_NOFOLLOW); '
            'os.write(f,data); os.fsync(f); os.close(f); os._exit(74)',
            str(route_path), new.hex()], cwd=self.project,
            capture_output=True, text=True, timeout=5,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})
        self.assertEqual(interrupted.returncode, 74)
        self.assertEqual(route_path.read_bytes(), new)
        self.assertEqual(inspect_journal(journal)['state'], 'pending')
        active_new = cas_recover(journal, pending['epoch'], new)
        with self.assertRaisesRegex(JournalEvidenceError, 'stale_journal_cas'):
            cas_activate(journal, pending['epoch'], new)
        self.assertEqual(active_new['selected']['sourceCommit'], second)
        decision = select_route(str(self.project))
        self.assertEqual(decision['state'], 'candidate')
        with self.assertRaisesRegex(JournalEvidenceError,
                                    'executing_code_outside_bundle'):
            verify_selected_route(decision)
        # A separate copied candidate proves content identity checks without
        # disrupting the active candidate needed for the rest of this scenario.
        bad = prepare_full_plugin_candidate(source, install / 'plugin-v2-bad',
                                            commit_id=second, vector_profile=profile)
        self._flip_entrypoint(bad['pluginRoot'])
        with self.assertRaises(BundleEvidenceError):
            inspect_full_plugin_candidate(bad['pluginRoot'])
        self.assertEqual(inspect_full_plugin_candidate(two['pluginRoot']), two)

        approval = install / 'development-vector-approval.json'
        approval_body = {'schema': 'laya-development-vector-approval-v1',
                         'testOwned': True, 'pluginRoot': two['pluginRoot'],
                         'sourceCommit': two['sourceCommit'],
                         'bundleManifestSha256': two['bundleManifestSha256'],
                         'ownershipManifestSha256': two['ownershipManifestSha256'],
                         'vectorProfile': profile}
        fd = os.open(approval, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, json.dumps(approval_body, sort_keys=True,
                                    separators=(',', ':')).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        child = subprocess.run([sys.executable, '-c',
            'import json,sys; sys.path.insert(0,sys.argv[1]); '
            'from laya_index_host_route import select_route; '
            'from laya_install_journal import verify_selected_route,'
            'verify_development_vector_approval; '
            'd=select_route(sys.argv[2]); p=verify_selected_route(d); '
            'v=verify_development_vector_approval(d,sys.argv[3]); '
            'print(json.dumps({"product":p["productRouteAllowed"],'
            '"status":p["status"],"development":v["developmentOnlyRouteAllowed"]}))',
            str(Path(two['pluginRoot']) / 'core'), str(self.project),
            str(approval)],
            check=True, capture_output=True, text=True, timeout=20,
            cwd=self.project, env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                                   'QMD_RECALL_LOG': '', 'QMD_SANDBOX': '1'})
        observed = json.loads(child.stdout)
        self.assertFalse(observed['product'])
        self.assertEqual(observed['status'], 'production_attestation_unbound')
        self.assertTrue(observed['development'])

        # Two real hosts keep separate cache directories. The same project
        # route must accept either exact installed version while preserving
        # the journal's single owner and rejecting a prior or altered cache.
        companion_parent = self.root / 'claude-host-cache'
        companion_parent.mkdir(mode=0o700)
        companion = prepare_full_plugin_candidate(
            source, companion_parent / 'plugin-v2-companion', commit_id=second,
            vector_profile=profile)
        self.assertNotEqual(companion['pluginRoot'], two['pluginRoot'])
        self.assertNotEqual(Path(companion['pluginRoot']).parent, journal.parent)
        self.assertEqual(companion['bundleManifestSha256'],
                         two['bundleManifestSha256'])
        self.assertNotEqual(companion['ownershipManifestSha256'],
                            two['ownershipManifestSha256'])
        alternate = ('import json,sys; sys.path.insert(0,sys.argv[1]); '
                     'from laya_index_host_route import select_route; '
                     'from laya_install_journal import verify_selected_route; '
                     'd=select_route(sys.argv[2]); p=verify_selected_route(d); '
                     'print(json.dumps({"state":d["state"],'
                     '"manifest":p["bundleManifestSha256"]}))')
        def prove_host(plugin):
            return subprocess.run(
                [sys.executable, '-c', alternate,
                 str(Path(plugin['pluginRoot']) / 'core'), str(self.project)],
                capture_output=True, text=True, timeout=20, cwd=self.project,
                env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                     'QMD_RECALL_LOG': '', 'QMD_SANDBOX': '1'})
        def assert_host_denied(plugin, reason):
            checked = prove_host(plugin)
            self.assertNotEqual(checked.returncode, 0)
            self.assertIn(reason, checked.stderr)
        for candidate in (two, companion, two):
            checked = prove_host(candidate)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertEqual(json.loads(checked.stdout)['manifest'],
                             two['bundleManifestSha256'])
        wrong_fingerprint = 'd' * 64
        wrong_profile = {**profile, 'embeddingFingerprint': wrong_fingerprint,
                         'profileId': hashlib.sha256(json.dumps(
                             [MODEL_ID, wrong_fingerprint, 2, 'cosine'],
                             sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode()).hexdigest()}
        different_profile = prepare_full_plugin_candidate(
            source, companion_parent / 'plugin-v2-other-profile',
            commit_id=second, vector_profile=wrong_profile)
        assert_host_denied(different_profile, 'executing_bundle_not_selected')
        assert_host_denied(one, 'executing_bundle_not_selected')
        host_marker = Path(companion['pluginRoot']) / '.in_use'
        host_marker.mkdir()
        (host_marker / '1').write_text('host-owned marker\n')
        self.assertEqual(prove_host(companion).returncode, 0)
        (host_marker / '1').chmod(0o666)
        assert_host_denied(companion, 'executing_code_outside_bundle')
        (host_marker / '1').chmod(0o600)
        (host_marker / 'helper.py').write_text('pass\n')
        assert_host_denied(companion, 'executing_code_outside_bundle')
        (host_marker / 'helper.py').unlink()
        (host_marker / '1').unlink()
        host_marker.rmdir()
        self._alter_core(companion['pluginRoot'])
        assert_host_denied(companion, 'executing_code_outside_bundle')

        # Real hosts copy the owner's plain files into their own cache without
        # the owner's metadata. The supported in-place seal proves every byte
        # and mode against the sealed owner; the route then accepts that copy.
        from laya_product_bundle import seal_companion_host_copy
        def host_copy(name):
            target = companion_parent / name
            shutil.copytree(two['pluginRoot'], target,
                            ignore=shutil.ignore_patterns('.laya-product'))
            return target
        cached = host_copy('codex-cache-copy')
        assert_host_denied({'pluginRoot': str(cached)}, 'executing_code_outside_bundle')
        sealed = seal_companion_host_copy(two['pluginRoot'], str(cached))
        self.assertEqual(sealed['pluginRoot'], str(cached))
        self.assertEqual(sealed['bundleManifestSha256'], two['bundleManifestSha256'])
        self.assertEqual(sealed['vectorProfile'], two['vectorProfile'])
        checked = prove_host(sealed)
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(json.loads(checked.stdout)['manifest'], two['bundleManifestSha256'])
        self.assertEqual(seal_companion_host_copy(two['pluginRoot'], str(cached))['pluginRoot'],
                         str(cached))
        self._alter_core(str(cached))
        assert_host_denied(sealed, 'executing_code_outside_bundle')
        # The supported CLI runs from the other host's copy against the journal owner.
        route_hint = install / 'reviewed-route.json'
        route_hint.write_text(json.dumps({'projectRoot': str(self.project)}))
        route_hint.chmod(0o600)
        via_cli = host_copy('claude-cache-copy')
        cli = subprocess.run(
            [sys.executable, '-B', str(via_cli / 'core' / 'setup_entry.py'), 'seal-host-copy',
             '--project', str(self.project), '--state-root', str(install)],
            capture_output=True, text=True, timeout=60, cwd=self.project,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'QMD_RECALL_LOG': ''})
        self.assertEqual(cli.returncode, 0, cli.stdout + cli.stderr)
        reported = json.loads(cli.stdout)
        self.assertEqual((reported['status'], reported['pluginRoot'], reported['ownerRoot']),
                         ('host_copy_sealed', str(via_cli), two['pluginRoot']))
        self.assertEqual(prove_host({'pluginRoot': str(via_cli)}).returncode, 0)
        other_project = self.root / 'other-project'
        other_project.mkdir(mode=0o700)
        refused = subprocess.run(
            [sys.executable, '-B', str(host_copy('mismatch-copy') / 'core' / 'setup_entry.py'),
             'seal-host-copy', '--project', str(other_project), '--state-root', str(install)],
            capture_output=True, text=True, timeout=60, cwd=self.project,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'QMD_RECALL_LOG': ''})
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('host_copy_project_mismatch', refused.stdout + refused.stderr)
        self.assertFalse((companion_parent / 'mismatch-copy' / '.laya-product').exists())
        route_hint.unlink()
        # Any drift fails closed and writes no metadata.
        def changed_byte(root):
            target = root / 'README.md'
            mode = stat.S_IMODE(target.stat().st_mode)
            target.chmod(mode | stat.S_IWUSR)
            with target.open('ab') as output:
                output.write(b'x')
            target.chmod(mode)
        def changed_mode(root):
            target = root / 'hooks' / 'run-hook'
            target.chmod(stat.S_IMODE(target.stat().st_mode) & ~0o111)
        def extra_file(root):
            (root / 'core' / 'planted_helper.py').write_text('pass\n')
        def foreign_metadata(root):
            shutil.copytree(Path(one['pluginRoot']) / '.laya-product', root / '.laya-product')
        for name, mutate, reason in (
                ('drift-byte', changed_byte, 'installed_blob_not_exact_bundle'),
                ('drift-mode', changed_mode, 'installed_blob_not_exact_bundle'),
                ('drift-extra', extra_file, 'installed_file_set_not_exact_bundle'),
                ('drift-foreign', foreign_metadata, '')):
            root = host_copy(name)
            mutate(root)
            with self.assertRaisesRegex(BundleEvidenceError, reason):
                seal_companion_host_copy(two['pluginRoot'], str(root))
            if name != 'drift-foreign':
                self.assertFalse((root / '.laya-product').exists())
        with self.assertRaisesRegex(BundleEvidenceError, 'companion_is_owner_root'):
            seal_companion_host_copy(two['pluginRoot'], two['pluginRoot'])

        # A development approval never opens product hooks. Each copied host
        # entrypoint must fail before QMD fallback or an index write.
        forbidden = self.root / 'legacy-fallback-invoked'
        fallback = self.root / 'forbid-legacy.sh'
        fallback.write_text('#!/bin/sh\nprintf invoked > ' + str(forbidden) + '\nexit 99\n')
        fallback.chmod(0o700)
        hook_env = {**os.environ, 'CLAUDE_PLUGIN_ROOT': two['pluginRoot'],
                    'QMD_BACKEND_MANAGER': str(fallback),
                    'QMD_CORE_RECALL_SCRIPT': str(fallback),
                    'QMD_RECALL_LOG': '', 'PYTHONDONTWRITEBYTECODE': '1'}
        for key in ('QMD_SANDBOX', 'CLAUDE_SANDBOX', 'CLAUDE_HEADLESS',
                    'CODEX_SANDBOX', 'GEMINI_SANDBOX', 'HERMES_SANDBOX',
                    'HERMES_HEADLESS', 'QMD_QUERY_FIXTURE'):
            hook_env.pop(key, None)
        hook = Path(two['pluginRoot']) / 'hooks' / 'run-hook'
        before_blocked_hooks = inspect_journal(journal)
        for host in ('claude', 'codex'):
            cases = [('recall', {'prompt': 'hello', 'cwd': str(self.project)}),
                     ('gate', {'hook_event_name': 'PreToolUse',
                               'cwd': str(self.project), 'tool_name': 'Write',
                               'tool_input': {'file_path': 'docs/live.md'}}),
                     ('index', {'hook_event_name': 'PostToolUse',
                                'cwd': str(self.project), 'tool_name': 'Write',
                                'tool_input': {'file_path': 'docs/live.md'}}),
                     ('update-queued', {'hook_event_name': 'SessionStart',
                                        'cwd': str(self.project)})]
            for action, details in cases:
                started = time.monotonic()
                result = subprocess.run(['bash', str(hook), action, host],
                    input=json.dumps(details), capture_output=True,
                    text=True, timeout=5, cwd=self.project, env=hook_env)
                elapsed = round((time.monotonic() - started) * 1000)
                print(f'TEMP_BLOCKED_{host.upper()}_{action.upper().replace("-", "_")}_MS={elapsed}',
                      flush=True)
                self.assertLess(elapsed, 5000)
                self.assertEqual(result.returncode, 3,
                                 action + ': ' + result.stdout + result.stderr)
                self.assertIn('QMD fallback refused', result.stderr)
        hermes = subprocess.run([sys.executable, '-c',
            'import json,sys; sys.path.insert(0,sys.argv[1]); '
            'from hermes_adapter.core_bridge import recall_context; '
            'print(json.dumps(recall_context("hello",cwd=sys.argv[2])))',
            two['pluginRoot'], str(self.project)], capture_output=True,
            text=True, timeout=20, cwd=self.project, env=hook_env)
        self.assertEqual(hermes.returncode, 0, hermes.stdout + hermes.stderr)
        self.assertNotIn('hello upgrade', json.loads(hermes.stdout)['context'])
        self.assertEqual(inspect_journal(journal), before_blocked_hooks)
        self.assertFalse(forbidden.exists())

        before_restore = inspect_journal(journal)
        with self.assertRaisesRegex(JournalEvidenceError, 'observed_route_mismatch'):
            cas_restore(journal, active_new['epoch'], old)
        route_path.write_bytes(old)
        with self.assertRaisesRegex(JournalEvidenceError, 'stale_journal_cas'):
            cas_restore(journal, pending['epoch'], old)
        self.assertEqual(inspect_journal(journal), before_restore)
        restored = cas_restore(journal, active_new['epoch'], old)
        self.assertEqual(restored['selected']['sourceCommit'], first)
        self.assertFalse(restored['newWriteSeen'])
        pending = cas_stage_update(journal, restored['epoch'], two, new)
        with self.assertRaisesRegex(JournalEvidenceError, 'stale_journal_cas'):
            cas_stage_update(journal, restored['epoch'], two, new)
        route_path.write_bytes(new)
        active_new = cas_activate(journal, pending['epoch'], new)
        self.assertFalse(active_new['newWriteSeen'])
        (self.docs / 'live.md').write_text('emerald heron after update\n',
                                          encoding='utf-8')
        # A stale disk route and a product-unapproved provider both block the
        # copied hook before enqueue/stage/publish.
        route_path.write_bytes(old)
        blocked_index = subprocess.run(['bash', str(hook), 'index', 'claude'],
            input=json.dumps({'hook_event_name': 'PostToolUse',
                              'cwd': str(self.project), 'tool_name': 'Write',
                              'tool_input': {'file_path': 'docs/live.md'}}),
            capture_output=True, text=True, timeout=5,
            cwd=self.project, env=hook_env)
        self.assertEqual(blocked_index.returncode, 3)
        self.assertIn('QMD fallback refused', blocked_index.stderr)
        self.assertFalse(inspect_journal(journal)['newWriteSeen'])
        with closing(LayaIndexA(db)) as index:
            self.assertEqual(index.db.execute(
                'SELECT gen FROM head WHERE scope=?', ('a' * 32,)).fetchone()[0],
                'seed')
        route_path.write_bytes(new)
        started = time.monotonic()
        development = subprocess.run([sys.executable, '-c',
            DEVELOPMENT_VECTOR_CHILD,
            str(Path(two['pluginRoot']) / 'core'), str(self.project),
            str(approval)],
            capture_output=True, text=True, timeout=20,
            cwd=self.project, env={**hook_env, 'QMD_SANDBOX': '1'})
        development_ms = round((time.monotonic() - started) * 1000)
        print('TEMP_DEVELOPMENT_VECTOR_SYNC_QUERY_MS=' + str(development_ms),
              flush=True)
        self.assertEqual(development.returncode, 0,
                         development.stdout + development.stderr)
        development_result = json.loads(development.stdout)
        self.assertEqual(development_result['changed'], 'indexed')
        self.assertEqual(development_result['repeated'], 'unchanged')
        self.assertEqual(development_result['paths'], ['live.md'])
        self.assertTrue(development_result['development'])
        written = inspect_journal(journal)
        self.assertTrue(written['newWriteSeen'])
        self.assertEqual(written['epoch'], active_new['epoch'] + 1)
        with closing(LayaIndexA(db)) as index:
            self.assertNotEqual(index.db.execute(
                'SELECT gen FROM head WHERE scope=?', ('a' * 32,)).fetchone()[0],
                'seed')
            self.assertGreaterEqual(index.inspect()['generations'], 3)
        route_path.write_bytes(old)
        with self.assertRaisesRegex(JournalEvidenceError, 'rollback_not_authorized'):
            cas_restore(journal, written['epoch'], old)
        route_path.write_bytes(new)
        self.assertEqual(inspect_journal(journal)['selected']['sourceCommit'], second)
        # Tamper with the active installed bytes only after all normal route
        # actions finish. The host proof must then refuse before query work.
        self._alter_core(two['pluginRoot'])
        with self.assertRaises(BundleEvidenceError):
            inspect_full_plugin_candidate(two['pluginRoot'])
        tampered = subprocess.run(['bash', str(hook), 'recall', 'claude'],
            input=json.dumps({'prompt': 'emerald heron',
                              'cwd': str(self.project)}),
            capture_output=True, text=True, timeout=5,
            cwd=self.project, env=hook_env)
        self.assertEqual(tampered.returncode, 3)
        self.assertIn('QMD fallback refused', tampered.stderr)
        self._global_untouched()

    def test_qmd_format_snapshot_stages_only_and_preserves_global_qmd(self):
        fixture = runpy.run_path(str(REPO / 'test' / 'fixtures' /
                                     'laya-qmd-migration-checks.py'))
        source = self.root / 'qmd-format.sqlite'
        fixture['make_source'](source, QMD_253)
        before = source.read_bytes()
        inventory = inspect_qmd(source, QMD_253, 'docs')
        backup = self.root / 'qmd-backup.sqlite'
        proof = backup_qmd(source, QMD_253, 'docs', backup)
        self.assertEqual(proof['source_digest'], inventory['digest'])
        self.assertTrue(verify_restore(backup, QMD_253, 'docs',
                                       inventory['digest'])['verified'])
        candidate = self.root / 'staged-only.sqlite'
        staged = prepare_qmd_candidate(
            backup, QMD_253, 'docs', {'live.md': 'astral manuscript'},
            candidate, 'a' * 32, 'migrated-1', migration_allowed=True,
            expected_collection_root='/synthetic/worktree-a',
            expected_source_digest=inventory['digest'])
        self.assertFalse(staged['cutover_ready'])
        with closing(LayaIndexA(candidate)) as index:
            self.assertEqual(index.inspect()['generations'], 1)
            self.assertIsNone(index.db.execute('SELECT gen FROM head').fetchone())
        self.assertEqual(source.read_bytes(), before)
        self._global_untouched()


if __name__ == '__main__':
    print('FORMAL_RELEASE_L1=NOT_RUN; DEVELOPMENT_VECTOR_ONLY=RUN',
          flush=True)
    unittest.main()
