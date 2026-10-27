"""Checks for core/context_learning/selection_port.py.

The port was extracted from recall's live-selection block. `legacy_*` below
are verbatim copies of the pre-extraction inline logic so equivalence is
asserted on outputs *and* raised exception type/message.
"""
import ast
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / 'core'))
from context_learning import selection_port as P
from context_learning.seam import _snapshot


# ── verbatim pre-extraction logic (recall.py live selection) ────────────────
def legacy_indexed(current_corpus):
    return ({(collection, relative): revision
        for collection, relative, revision in current_corpus['material']['activeDocuments']}
        if current_corpus else {})


def legacy_pool_reason(filtered_results, primary_limit, is_wiki):
    if not filtered_results:
        return 'no_candidates'
    elif any(not is_wiki(r.get('_collection', ''))
             for r in filtered_results[:primary_limit]):
        return 'non_wiki_pool'
    return None


def legacy_rows(filtered_results, primary_limit, resolve, snapshot, indexed_hashes):
    model_rows = []
    for hit in filtered_results[:primary_limit]:
        path = resolve(hit)
        if path is None:
            raise ValueError('candidate_path_unavailable')
        snapped = snapshot(path)
        if snapped is None:
            raise ValueError('candidate_snapshot_unavailable')
        revision, content = snapped
        collection, separator, relative = hit['file'].removeprefix('qmd://').partition('/')
        if not separator or indexed_hashes.get((collection, relative)) != revision['sha256']:
            raise ValueError('candidate_index_stale')
        model_rows.append({'id': hit['file'].removeprefix('qmd://'),
                           'revision_sha256': revision['sha256'],
                           'source_text': content.decode('utf8')})
    return model_rows


def legacy_apply(filtered_results, primary_limit, selected_ids):
    by_id = {r['file'].removeprefix('qmd://'): r
             for r in filtered_results[:primary_limit]}
    return [by_id[cid] for cid in selected_ids]


def outcome(fn, *args, **kwargs):
    try:
        return ('ok', fn(*args, **kwargs))
    except Exception as exc:  # compare type + message, never swallow silently
        return ('raise', type(exc).__name__, str(exc))


def tree_state(root):
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            p = os.path.join(dirpath, name)
            st = os.lstat(p)
            out[p] = (st.st_mode, st.st_size, st.st_mtime_ns)
    return out


class Cards:
    """Real wiki card files read through the production seam._snapshot."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / 'wiki').mkdir()
        self.hashes = {}

    def add(self, name, body):
        path = self.root / 'wiki' / name
        path.write_bytes(body)
        snap = _snapshot(path)
        self.hashes[('wiki', name)] = snap[0]['sha256']
        return path

    def resolve(self, hit):
        _, _, rel = hit['file'].removeprefix('qmd://').partition('/')
        path = self.root / 'wiki' / rel
        return path if path.exists() else None

    def close(self):
        self.tmp.cleanup()


class SelectionPortTests(unittest.TestCase):
    def setUp(self):
        self.cards = Cards()
        self.cards.add('a.md', '---\ntitle: A\n---\nalpha\n'.encode())
        self.cards.add('b.md', '---\ntitle: B\n---\n베타\n'.encode())
        self.pool = [{'file': 'qmd://wiki/a.md', '_collection': 'wiki'},
                     {'file': 'wiki/b.md', '_collection': 'wiki'}]

    def tearDown(self):
        self.cards.close()

    def test_indexed_revisions_matches_legacy(self):
        corpus = {'material': {'activeDocuments': [['wiki', 'a.md', 'h1'], ('raw', 'x/y.md', 'h2')]}}
        for value in (None, {}, corpus):
            self.assertEqual(P.indexed_revisions(value), legacy_indexed(value))
        self.assertEqual(P.indexed_revisions(corpus), {('wiki', 'a.md'): 'h1', ('raw', 'x/y.md'): 'h2'})

    def test_pool_rejection_matches_legacy(self):
        is_wiki = lambda c: c == 'wiki'
        cases = [
            ([], 15),
            (self.pool, 15),
            (self.pool + [{'file': 'raw/c.md', '_collection': 'raw'}], 15),
            (self.pool + [{'file': 'raw/c.md', '_collection': 'raw'}], 2),  # outside window
            ([{'file': 'x/c.md'}], 15),  # missing _collection → ''
            (self.pool, 0),  # empty window but non-empty pool
        ]
        for pool, limit in cases:
            self.assertEqual(P.pool_rejection(pool, limit, is_wiki),
                             legacy_pool_reason(pool, limit, is_wiki), (pool, limit))
        self.assertEqual(P.pool_rejection([], 15, is_wiki), 'no_candidates')
        self.assertEqual(P.pool_rejection(cases[2][0], 15, is_wiki), 'non_wiki_pool')
        self.assertIsNone(P.pool_rejection(cases[3][0], 2, is_wiki))
        seen = []
        P.pool_rejection([{'file': 'x/c.md'}], 15, lambda c: seen.append(c) or True)
        self.assertEqual(seen, [''])

    def test_candidate_rows_success_and_equivalence(self):
        rows = P.candidate_rows(self.pool, 15, resolve_path=self.cards.resolve,
                                snapshot=_snapshot, indexed=self.cards.hashes)
        self.assertEqual([r['id'] for r in rows], ['wiki/a.md', 'wiki/b.md'])
        self.assertEqual(rows[1]['source_text'], '---\ntitle: B\n---\n베타\n')
        self.assertEqual(set(rows[0]), {'id', 'revision_sha256', 'source_text'})
        self.assertEqual(rows, legacy_rows(self.pool, 15, self.cards.resolve, _snapshot, self.cards.hashes))
        # limit bounds the scored window exactly as before
        self.assertEqual(len(P.candidate_rows(self.pool, 1, resolve_path=self.cards.resolve,
                                              snapshot=_snapshot, indexed=self.cards.hashes)), 1)

    def test_failure_codes_match_legacy(self):
        bad_utf8 = self.cards.add('c.md', b'\xff\xfe broken')
        self.assertTrue(bad_utf8.exists())
        stale = {**self.cards.hashes, ('wiki', 'b.md'): 'other'}
        cases = [
            ([{'file': 'qmd://wiki/missing.md'}], self.cards.hashes, _snapshot),
            (self.pool, self.cards.hashes, lambda p: None),
            (self.pool, stale, _snapshot),
            ([{'file': 'qmd://noslash'}], self.cards.hashes,
             lambda p: _snapshot(self.cards.root / 'wiki' / 'a.md')),
            ([{'file': 'wiki/c.md'}], self.cards.hashes, _snapshot),
            ([{'_collection': 'wiki'}], self.cards.hashes, _snapshot),
        ]
        resolve = lambda hit: (self.cards.root / 'wiki' / 'a.md') if hit.get('file') == 'qmd://noslash' else self.cards.resolve(hit)
        expected = ['candidate_path_unavailable', 'candidate_snapshot_unavailable',
                    'candidate_index_stale', 'candidate_index_stale', 'UnicodeDecodeError', 'KeyError']
        for (pool, indexed, snap), want in zip(cases, expected):
            new = outcome(P.candidate_rows, pool, 15, resolve_path=resolve, snapshot=snap, indexed=indexed)
            old = outcome(legacy_rows, pool, 15, resolve, snap, indexed)
            self.assertEqual(new, old, want)
            self.assertEqual(new[0], 'raise')
            self.assertIn(want, (new[1], new[2]))
            # every error class recall maps to `candidate_unavailable`
            self.assertIn(new[1], ('ValueError', 'UnicodeDecodeError', 'KeyError'))

    def test_all_or_nothing_and_call_order(self):
        calls = []
        def resolve(hit):
            calls.append(('resolve', hit['file']))
            return self.cards.resolve(hit)
        def snap(path):
            calls.append(('snapshot', path.name))
            return None if path.name == 'a.md' else _snapshot(path)
        with self.assertRaises(ValueError):
            P.candidate_rows(self.pool, 15, resolve_path=resolve, snapshot=snap, indexed=self.cards.hashes)
        # stops at the first failing hit; the second card is never read
        self.assertEqual(calls, [('resolve', 'qmd://wiki/a.md'), ('snapshot', 'a.md')])

    def test_apply_selection_matches_legacy(self):
        for ids, limit in ((['wiki/b.md', 'wiki/a.md'], 15), ([], 15), (['wiki/a.md'], 1)):
            self.assertEqual(P.apply_selection(self.pool, limit, ids), legacy_apply(self.pool, limit, ids))
        self.assertIs(P.apply_selection(self.pool, 15, ['wiki/b.md'])[0], self.pool[1])
        for ids, limit in ((['wiki/zzz.md'], 15), (['wiki/b.md'], 1)):
            self.assertEqual(outcome(P.apply_selection, self.pool, limit, ids),
                             outcome(legacy_apply, self.pool, limit, ids))
            self.assertEqual(outcome(P.apply_selection, self.pool, limit, ids)[1], 'KeyError')

    def test_port_is_read_only(self):
        before = tree_state(self.cards.root)
        pool_before = [dict(h) for h in self.pool]
        P.indexed_revisions({'material': {'activeDocuments': []}})
        P.pool_rejection(self.pool, 15, lambda c: True)
        P.candidate_rows(self.pool, 15, resolve_path=self.cards.resolve,
                         snapshot=_snapshot, indexed=self.cards.hashes)
        P.apply_selection(self.pool, 15, ['wiki/a.md'])
        self.assertEqual(tree_state(self.cards.root), before)
        self.assertEqual(self.pool, pool_before)

    def test_port_imports_nothing_that_writes(self):
        source = Path('core/context_learning/selection_port.py').read_text(encoding='utf8')
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add(node.module or '')
        # Published-row validation adds only pure hashing and syntax checks.
        self.assertEqual(imported, {'__future__', 'hashlib', 're'})
        # Code after the module docstring (which names the excluded writers).
        body = source.split('"""', 2)[-1]
        for token in ('open(', 'write', 'mkdir', 'unlink', 'replace(', 'rename', 'subprocess'):
            self.assertNotIn(token, body, token)

# ── selection_read: the coordinator recall calls for live selection ─────────
import hook_budget
if __name__ == '__main__':
    unittest.main(verbosity=2)
