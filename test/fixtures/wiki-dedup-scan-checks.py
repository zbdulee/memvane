"""Retroactive dedup scan on a real, fixture-built laya-db (no model or runtime)."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))

from laya_index_a import LayaIndexA, _digest as manifest_digest  # noqa: E402
from laya_index_vector import LayaVectorIndex, profile_identity, qmd_markdown_title  # noqa: E402
import wiki_dedup_scan as scan  # noqa: E402
import wiki_selected_laya  # noqa: E402

SCOPE = '7' * 32
MODEL = 'fixture-only'
FINGERPRINT = 'f' * 64
CLAIM = 'e' * 64
WIKI = 'wiki'


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def card(title, fact, status='generated'):
    return ('---\ntitle: %s\nstatus: %s\ncreatedBy: qmd-auto-context\n---\n\n%s\n'
            % (title, status, fact))


class Project:
    """A selected-route project whose wiki cards are published with chosen vectors."""

    def __init__(self, base: Path, cards: dict, *, max_pairs=10):
        self.root = base / 'project'
        self.wiki = self.root / '.auto-context' / 'wiki'
        self.wiki.mkdir(parents=True)
        (self.root / '.auto-context' / 'settings.json').write_text(json.dumps({
            'indexing': True, 'collections': ['raw', WIKI],
            'collectionPaths': {'raw': 'docs', WIKI: '.auto-context/wiki'},
            'collectionRoles': {'raw': 'raw', WIKI: 'wiki'},
            'wikiPath': '.auto-context/wiki',
            'compile': {'mode': 'auto-wiki',
                        'semanticDedup': {'maxPairsPerScan': max_pairs}}}))
        state = base / 'laya-state'
        state.mkdir(mode=0o700)
        self.index_path = state / 'index.sqlite'
        self.index = LayaIndexA(self.index_path, create=True)
        self.vectors = LayaVectorIndex(self.index)
        self.profile_id = profile_identity(MODEL, FINGERPRINT, 2)
        self.vectors.register_profile(MODEL, FINGERPRINT, 2)
        self.head = None
        self.epoch = 0
        self.decision = {
            'state': 'candidate', 'projectRoot': str(self.root),
            'dbPath': str(self.index_path), 'scopeId': SCOPE,
            'dbIncarnation': self.index.incarnation(),
            'vectorProfile': {'profileId': self.profile_id, 'modelId': MODEL,
                              'embeddingFingerprint': FINGERPRINT, 'dimension': 2,
                              'metric': 'cosine'},
            'collections': [
                {'name': 'raw', 'role': 'raw', 'sourceRoot': str(self.root / 'docs')},
                {'name': WIKI, 'role': 'wiki', 'sourceRoot': str(self.wiki)}]}
        self.publish(cards)

    def publish(self, cards):
        """Stage, vector-seal and publish one generation; write the same bytes to disk."""
        self.epoch += 1
        gen = 'gen-%d' % self.epoch
        documents = {WIKI + '/' + rel: text for rel, (text, _vector) in cards.items()}
        rows = [(path, text, True) for path, text in sorted(documents.items())]
        manifest = manifest_digest(SCOPE, [(path, sha(text), 1) for path, text, _ in rows])
        assert self.index.stage(gen, SCOPE, rows, base=self.head, source_manifest=manifest,
                                claim_digest=CLAIM, claim_epoch=self.epoch) == 'prepared'
        for rel, (text, vector) in cards.items():
            path = WIKI + '/' + rel
            self.vectors.insert_chunk_set(sha(text), qmd_markdown_title(text, path),
                                          self.profile_id,
                                          [{'seq': 0, 'pos': 0, 'vector': vector}])
        self.vectors.seal_generation(gen, self.profile_id)
        assert self.index.publish(self.head, gen, 'claim-%d' % self.epoch,
                                  source_manifest=manifest, claim_digest=CLAIM,
                                  claim_epoch=self.epoch, require_vector=True) == 'published'
        self.head = gen
        for rel, (text, _vector) in cards.items():
            target = self.wiki / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)

    def route(self):
        stack = ExitStack()
        bound = (dict(self.decision), {'productRouteAllowed': True})
        stack.enter_context(patch.object(wiki_selected_laya, 'selected', return_value=bound))
        stack.enter_context(patch.object(wiki_selected_laya, 'recheck', return_value=None))
        return stack

    @property
    def queue(self):
        return self.root / '.auto-context' / 'compile' / 'dedup-needed.jsonl'

    def queued(self):
        if not self.queue.exists():
            return []
        return [json.loads(line) for line in self.queue.read_text().splitlines() if line]

    def run(self):
        with self.route():
            return scan.run(str(self.root))


ALPHA = card('Alpha bridge', 'The north bridge closes at 22:00 for repairs.')
ALPHA_COPY = card('Alpha bridge closure', 'North bridge repairs close it at 22:00.')
BETA = card('Beta harbor', 'The harbor ferry leaves every 40 minutes.')
GAMMA = card('Gamma market', 'The market opens on weekends only.')


def standard():
    return {'entities/alpha.md': (ALPHA, [1.0, 0.0]),
            'entities/alpha-copy.md': (ALPHA_COPY, [0.99, 0.1]),
            'entities/beta.md': (BETA, [0.0, 1.0]),
            'entities/gamma.md': (GAMMA, [0.6, 0.8])}


class Checks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='wiki-dedup-scan-', dir='/tmp')
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        env = patch.dict(os.environ, {
            'HOME': str(self.base / 'home'),
            'QMD_SYNC_STATE_DIR': str(self.base / 'scan-state'),
            'QMD_WIKI_DEDUP_LOG': str(self.base / 'wiki-dedup.log'),
            'QMD_SANDBOX': ''})
        env.start()
        self.addCleanup(env.stop)

    def project(self, cards=None, **kwargs):
        project = Project(self.base, standard() if cards is None else cards, **kwargs)
        self.addCleanup(project.index.close)
        return project

    def test_queues_only_the_near_duplicate_pair_once(self):
        project = self.project()
        result = project.run()
        self.assertEqual(result['state'], 'scanned', result)
        self.assertEqual(result['cards'], 4)
        rows = project.queued()
        self.assertEqual(len(rows), 1, rows)
        self.assertEqual({rows[0]['pageA'], rows[0]['pageB']},
                         {'entities/alpha.md', 'entities/alpha-copy.md'})
        self.assertGreaterEqual(rows[0]['score'], 0.9)
        # The snapshot records every compared card: nothing is compared or queued again.
        again = project.run()
        self.assertEqual((again['compared'], again['queued']), (0, 0), again)
        self.assertEqual(len(project.queued()), 1)

    def test_resolved_queue_is_not_refilled_until_a_card_changes(self):
        project = self.project()
        project.run()
        project.queue.write_text('')
        self.assertEqual(project.run()['queued'], 0)
        changed = standard()
        changed['entities/alpha-copy.md'] = (ALPHA_COPY + '\nAdded detail.\n', [0.98, 0.12])
        project.publish(changed)
        result = project.run()
        self.assertEqual((result['compared'], result['queued']), (1, 1), result)

    def test_skip_ledger_suppresses_unchanged_bodies(self):
        project = self.project()
        ledger = project.root / '.auto-context' / 'compile' / 'dedup-skipped.jsonl'
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({
            'pageA': 'entities/alpha-copy.md', 'pageB': 'entities/alpha.md',
            'pageAHash': scan.body_hash(ALPHA_COPY), 'pageBHash': scan.body_hash(ALPHA)}) + '\n')
        result = project.run()
        self.assertEqual(result['queued'], 0, result)
        self.assertGreaterEqual(result['suppressed'], 1)
        self.assertEqual(project.queued(), [])

    def test_card_edited_after_publish_waits_for_the_next_publish(self):
        project = self.project()
        (project.wiki / 'entities/alpha-copy.md').write_text(ALPHA_COPY + '\nUnpublished edit.\n')
        result = project.run()
        self.assertEqual(result['queued'], 0, result)
        self.assertEqual(project.queued(), [])

    def test_superseded_card_is_out_of_scope(self):
        cards = standard()
        cards['entities/alpha-copy.md'] = (card('Alpha bridge closure',
                                                'North bridge repairs close it at 22:00.',
                                                status='superseded'), [0.99, 0.1])
        project = self.project(cards)
        self.assertEqual(project.run()['queued'], 0)

    def test_pair_cap_leaves_the_rest_pending_for_the_next_run(self):
        cards = standard()
        cards['entities/beta-copy.md'] = (card('Beta ferry', 'Ferries leave the harbor every 40 minutes.'),
                                          [0.1, 0.99])
        project = self.project(cards, max_pairs=1)
        first = project.run()
        self.assertEqual(first['queued'], 1, first)
        self.assertGreater(first['pending'], 0)
        second = project.run()
        self.assertEqual(second['queued'], 1, second)
        pairs = {frozenset((row['pageA'], row['pageB'])) for row in project.queued()}
        self.assertEqual(pairs, {frozenset(('entities/alpha.md', 'entities/alpha-copy.md')),
                                 frozenset(('entities/beta.md', 'entities/beta-copy.md'))})

    def test_unavailable_route_or_store_writes_nothing(self):
        project = self.project()
        with patch.object(wiki_selected_laya, 'selected',
                          side_effect=ValueError('selected_compile_route_unavailable')):
            self.assertEqual(scan.run(str(project.root))['state'], 'deferred')
        project.decision['dbIncarnation'] = '0' * 32
        self.assertEqual(project.run()['state'], 'deferred')
        self.assertFalse(project.queue.exists())
        self.assertFalse((self.base / 'scan-state').exists())

    def test_compile_off_skips(self):
        project = self.project()
        settings = project.root / '.auto-context' / 'settings.json'
        value = json.loads(settings.read_text())
        value['compile']['mode'] = 'off'
        settings.write_text(json.dumps(value))
        self.assertEqual(project.run()['state'], 'skipped')
        self.assertFalse(project.queue.exists())

    def test_lifecycle_child_runs_the_scan_without_changing_its_result(self):
        import wiki_lifecycle
        calls = []
        with patch.object(wiki_lifecycle, 'run', side_effect=RuntimeError('lifecycle failed')), \
                patch.object(scan, 'run', side_effect=calls.append), \
                patch.object(sys, 'argv', ['wiki_lifecycle.py', '--cwd', str(self.base)]):
            self.assertEqual(wiki_lifecycle.main(), 0)
        self.assertEqual(calls, [str(self.base)])
        with patch.object(scan, 'run', side_effect=RuntimeError('scan failed')), \
                patch.object(wiki_lifecycle, 'run', return_value={'retired': 0}), \
                patch.object(sys, 'argv', ['wiki_lifecycle.py', '--cwd', str(self.base), '--json']), \
                patch('builtins.print') as printed:
            self.assertEqual(wiki_lifecycle.main(), 0)
        printed.assert_called_once_with(json.dumps({'retired': 0}, ensure_ascii=False))


if __name__ == '__main__':
    unittest.main()
