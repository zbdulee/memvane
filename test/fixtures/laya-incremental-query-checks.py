"""Synthetic incremental read checks; all vectors are fixed fixture floats."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

from laya_index_a import LayaIndexA
from laya_index_vector import LayaVectorIndex, profile_identity, qmd_markdown_title
import laya_incremental_docs as incremental
from laya_incremental_docs_query import query_incremental
from laya_host_response import render_context
from laya_index_host_route import verify_preview_route


SCOPE = 'c' * 32
INSTALL = 'a' * 32
FINGERPRINT = 'f' * 64
MODEL = 'fixture-only'


def digest(body):
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


class IncrementalReadChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='laya-incremental-query-')
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        self.project = root / 'project'
        self.wiki = self.project / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        self.state = root / 'state'
        self.state.mkdir(mode=0o700)
        self.state.chmod(0o700)
        self.index_path = self.state / 'index.sqlite'
        self.index = LayaIndexA(self.index_path, create=True)
        self.addCleanup(self.index.close)
        self.profile_id = profile_identity(MODEL, FINGERPRINT, 2)
        self.vectors = LayaVectorIndex(self.index)
        self.vectors.register_profile(MODEL, FINGERPRINT, 2)
        self.profile = {'profileId': self.profile_id, 'modelId': MODEL,
                        'embeddingFingerprint': FINGERPRINT, 'dimension': 2,
                        'metric': 'cosine', 'approvedProviderId': 'fixture',
                        'providerArtifactSha256': 'e' * 64,
                        'providerRelativePath': 'core/fixture.py'}
        self.identity = {'stateRoot': str(self.state), 'projectRoot': str(self.project),
                         'installId': INSTALL, 'scopeId': SCOPE,
                         'candidateDb': str(self.index_path),
                         'dbIncarnation': self.index.incarnation(),
                         'vectorProfileId': self.profile_id}
        self.decision = {'state': 'candidate', 'projectRoot': str(self.project),
                         'journalPath': str(self.state / 'install-journal.sqlite'),
                         'scopeId': SCOPE, 'installId': INSTALL,
                         'dbPath': str(self.index_path),
                         'dbIncarnation': self.identity['dbIncarnation'],
                         'vectorProfile': self.profile,
                         'collections': [
                             {'name': 'raw', 'role': 'raw',
                              'sourceRoot': str(self.project)},
                             {'name': 'wiki', 'role': 'wiki',
                              'sourceRoot': str(self.wiki)}],
                         'recallSettings': {'lexicalPatterns': [], 'skipPaths': [],
                                            'minScore': 0, 'topN': 5}}

    def fixture_provider(self, _prompt, _profile):
        return {'profileId': self.profile_id, 'approvedProviderId': 'fixture',
                'providerArtifactSha256': 'e' * 64, 'vector': [1.0, 0.0]}

    def test_old_head_path_is_excluded_before_fts_and_vector_limit(self):
        old = '# Old\nneedle needle\n'
        other = '# Other\nneedle\n'
        self.assertEqual(self.index.stage('head', SCOPE,
            [('wiki/old.md', old, True), ('wiki/other.md', other, True)]), 'prepared')
        for path, body in [('wiki/old.md', old), ('wiki/other.md', other)]:
            title = qmd_markdown_title(body, path)
            self.vectors.insert_chunk_set(digest(body), title, self.profile_id,
                                          [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}])
        self.vectors.seal_generation('head', self.profile_id)
        self.assertEqual(self.index.publish(None, 'head', 'claim',
                                            require_vector=True), 'published')
        token, _ = self.index.pin(SCOPE)
        try:
            lexical = self.index.search_many(token, ('needle',), 1,
                                              excluded_paths=('wiki/old.md',))[0]
            vector = self.vectors.search_exact(
                token, [1.0, 0.0], model_id=MODEL,
                embedding_fingerprint=FINGERPRINT, dimension=2, limit=1,
                excluded_paths=('wiki/old.md',))
            self.assertEqual([row['path'] for row in lexical], ['wiki/other.md'])
            self.assertEqual([row['path'] for row in vector], ['wiki/other.md'])
        finally:
            self.index.release(token)

    def test_stale_incremental_revision_still_shadows_old_head(self):
        old = '# Old\nold term\n'
        new = '# New\nunique new term\n'
        self.assertEqual(self.index.stage('head', SCOPE,
                                          [('wiki/a.md', old, True)]), 'prepared')
        self.assertEqual(self.index.publish(None, 'head', 'claim'), 'published')
        self.assertEqual(self.index.stage('future', SCOPE,
                                          [('wiki/a.md', new, True)]), 'prepared')
        title = qmd_markdown_title(new, 'wiki/a.md')
        self.vectors.insert_chunk_set(digest(new), title, self.profile_id,
                                      [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}])
        with incremental._open(str(self.state)) as side:
            incremental._setup(side, self.identity)
            side.execute('BEGIN IMMEDIATE')
            rowid = side.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                 ('wiki/a.md', digest(new), title, new,
                                  self.profile_id, 1, 'scan')).lastrowid
            side.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                         (rowid, 'wiki/a.md', title, new))
            side.commit()
        (self.wiki / 'a.md').write_text(new, encoding='utf-8')
        with mock.patch('laya_host_response._verified_wiki_row', return_value=True), \
                mock.patch('wiki_freshness.unresolved_pending_refreshes_strict',
                           return_value=[]):
            rows, titles, _locations, shadow, base = query_incremental(
                'unique', self.decision, {'vectorProfile': self.profile},
                self.fixture_provider)
            self.assertEqual((shadow, base), (('wiki/a.md',), 'head'))
            self.assertEqual([row['path'] for row in rows], ['wiki/a.md'])
            # A single general term is a lone survivor, so the shared
            # selected/preview body gate permits only the verified title.
            self.assertEqual(titles, frozenset({'wiki/a.md'}))
            vector_rows, vector_titles, _locations, _shadow, _base = query_incremental(
                'vectorsemanticnonce', self.decision,
                {'vectorProfile': self.profile}, self.fixture_provider)
            self.assertEqual([row['path'] for row in vector_rows], ['wiki/a.md'])
            self.assertEqual(vector_titles, frozenset({'wiki/a.md'}))
            # A newer edit has no completed vector set yet. It must not reveal
            # either the obsolete incremental row or the older head row.
            (self.wiki / 'a.md').write_text('# Third\nnot embedded\n', encoding='utf-8')
            rows, _titles, _locations, shadow, base = query_incremental(
                'unique', self.decision, {'vectorProfile': self.profile},
                self.fixture_provider)
            self.assertEqual(rows, [])
            self.assertEqual((shadow, base), (('wiki/a.md',), 'head'))
            # An exclusion appended after the sidecar commit still applies.
            self.index.exclude(SCOPE, path='wiki/a.md')
            rows, _titles, _locations, shadow, _base = query_incremental(
                'unique', self.decision, {'vectorProfile': self.profile},
                self.fixture_provider)
            self.assertEqual(rows, [])
            self.assertEqual(shadow, ('wiki/a.md',))
            # A hash exclusion applies to another completed path too, while
            # both old paths remain shadowed for the selected head query.
            (self.wiki / 'b.md').write_text(new, encoding='utf-8')
            with incremental._open(str(self.state)) as side:
                incremental._setup(side, self.identity)
                side.execute('BEGIN IMMEDIATE')
                rowid = side.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                     ('wiki/b.md', digest(new), title, new,
                                      self.profile_id, 1, 'scan')).lastrowid
                side.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                             (rowid, 'wiki/b.md', title, new))
                side.commit()
            self.index.exclude(SCOPE, hash_=digest(new))
            rows, _titles, _locations, shadow, _base = query_incremental(
                'unique', self.decision, {'vectorProfile': self.profile},
                self.fixture_provider)
            self.assertEqual(rows, [])
            self.assertEqual(shadow, ('wiki/a.md', 'wiki/b.md'))

    def test_empty_preview_reports_building_without_selected_route(self):
        decision = {**self.decision, 'state': 'preview', 'buildPhase': 'building'}
        context = render_context(decision, [], limit=5, ranking='hybrid_rrf')
        self.assertIn('0 completed wiki documents; initial build building', context)

    def test_sealed_but_inactive_preview_keeps_completed_wiki(self):
        body = '# Sealed\nuniquesealedterm\n'
        path = 'wiki/a.md'
        self.assertEqual(self.index.stage('head', SCOPE, [(path, body, True)]),
                         'prepared')
        self.assertEqual(self.index.publish(None, 'head', 'claim'), 'published')
        title = qmd_markdown_title(body, path)
        self.vectors.insert_chunk_set(digest(body), title, self.profile_id,
                                      [{'seq': 0, 'pos': 0, 'vector': [1.0, 0.0]}])
        with incremental._open(str(self.state)) as side:
            incremental._setup(side, self.identity)
            side.execute('BEGIN IMMEDIATE')
            rowid = side.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                 (path, digest(body), title, body,
                                  self.profile_id, 1, 'scan')).lastrowid
            side.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                         (rowid, path, title, body))
            side.commit()
        (self.wiki / 'a.md').write_text(body, encoding='utf-8')
        preview = {**self.decision, 'state': 'preview',
                   'previewStateRoot': str(self.state), 'buildPhase': 'sealed'}
        with mock.patch('laya_host_response._verified_wiki_row', return_value=True), \
                mock.patch('wiki_freshness.unresolved_pending_refreshes_strict',
                           return_value=[]):
            rows, _titles, _locations, _shadow, base = query_incremental(
                'uniquesealedterm', preview, {'vectorProfile': self.profile},
                self.fixture_provider)
        self.assertEqual(base, 'head')
        self.assertEqual([row['path'] for row in rows], [path])

    def test_filepath_is_a_lexical_signal(self):
        with incremental._open(str(self.state)) as side:
            incremental._setup(side, self.identity)
            side.execute('BEGIN IMMEDIATE')
            rowid = side.execute('INSERT INTO docs VALUES(?,?,?,?,?,?,?)',
                                 ('wiki/codenameunique.md', digest('plain'), 'Plain',
                                  'plain', self.profile_id, 1, 'scan')).lastrowid
            side.execute('INSERT INTO docs_fts(rowid,path,title,body) VALUES(?,?,?,?)',
                         (rowid, 'wiki/codenameunique.md', 'Plain', 'plain'))
            side.commit()
            paths = side.execute('SELECT path FROM docs_fts '
                                 'WHERE docs_fts MATCH ?', ('codenameunique',)).fetchall()
            self.assertEqual(paths, [('wiki/codenameunique.md',)])

    def test_initial_preview_requires_pending_unselected_first_write(self):
        decision = {**self.decision, 'state': 'preview',
                    'previewStateRoot': str(self.state),
                    'previewRouteSha256': 'b' * 64,
                    'pluginRoot': str(self.state / 'plugin'),
                    'generation': 'g-' + 'a' * 32,
                    'bundleManifestSha256': 'd' * 64,
                    'ownershipManifestSha256': 'e' * 64}
        pending = {key: decision[key] for key in
                   ('pluginRoot', 'installId', 'generation',
                    'bundleManifestSha256', 'ownershipManifestSha256')}
        pending['vectorProfile'] = self.profile
        journal = {'state': 'candidate_ready', 'selected': None,
                   'newWriteSeen': False, 'pendingRouteSha256': 'b' * 64,
                   'scopeId': SCOPE, 'projectRoot': str(self.project),
                   'pending': pending, 'epoch': 1}
        preflight = {'pendingRouteSha256': 'b' * 64,
                     'providerManifest': {'synthetic': True}}
        with mock.patch('laya_index_host_route.select_route', return_value=decision), \
                mock.patch('laya_initial_build.lookup', return_value={
                    'identity': {'stateRoot': str(self.state)}, 'phase': 'building'}), \
                mock.patch('laya_install_journal.inspect_journal',
                           side_effect=lambda _path: journal), \
                mock.patch('laya_install_journal.preflight_product_candidate',
                           return_value=preflight):
            self.assertEqual(verify_preview_route(decision)['buildPhase'], 'building')
            journal['newWriteSeen'] = True
            with self.assertRaisesRegex(ValueError, 'initial_preview_journal_mismatch'):
                verify_preview_route(decision)


if __name__ == '__main__':
    unittest.main()
