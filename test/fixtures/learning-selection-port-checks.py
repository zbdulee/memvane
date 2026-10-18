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
        self.assertEqual(imported, {'__future__'})
        # Code after the module docstring (which names the excluded writers).
        body = source.split('"""', 2)[-1]
        for token in ('open(', 'write', 'mkdir', 'unlink', 'replace(', 'rename', 'subprocess'):
            self.assertNotIn(token, body, token)

    def test_recall_uses_one_read_entry_and_keeps_diagnostics(self):
        recall = Path('core/recall.py').read_text(encoding='utf8')
        reader = Path('core/context_learning/selection_read.py').read_text(encoding='utf8')
        # One entry from recall; the port is reached only through the coordinator.
        self.assertEqual(recall.count('selection_read.select('), 1)
        self.assertNotIn('selection_port.', recall)
        self.assertNotIn('from context_learning.live_select import choose', recall)
        for call in ('selection_port.indexed_revisions(', 'selection_port.pool_rejection(',
                     'selection_port.candidate_rows(', 'selection_port.apply_selection('):
            self.assertEqual(reader.count(call), 1, call)
        # The inline copies are gone, so they cannot drift.
        self.assertNotIn("raise ValueError('candidate_index_stale')", recall)
        self.assertNotIn("raise ValueError('candidate_index_stale')", reader)
        # The pre-query plan stays before the read; the selection log after it.
        plan = recall.index('live_plan = plan_live_pool(')
        read = recall.index('selection_read.select(')
        log = recall.index("event_override='qmd_laya_selection'")
        self.assertLess(plan, read)
        self.assertLess(read, log)
        # State resolution (may mkdir) and the selector stay out of the pure port.
        self.assertIn('state = _private_state(config, card_root)', reader)
        self.assertIn('= choose(', reader)


# ── selection_read: the coordinator recall calls for live selection ─────────
import hook_budget
from contextlib import ExitStack
from unittest import mock
from context_learning import (adaptive_pool, candidate_apply, compact_input, corpus,
                              learning_profile, live_select, seam, shared)
from context_learning import selection_read as R

DEADLINE = 123.0


def legacy_select(filtered_results, primary_limit, top_n, *, config, card_roots, qmd_paths,
                  prompt, is_wiki, resolve, shared_trial, shared_profile, live_plan,
                  internal_edit, shared_options, deadline):
    """Verbatim pre-extraction recall block (hook_budget.deadline → deadline)."""
    final_results = filtered_results[:min(top_n, 3)]
    live_reason = 'disabled'
    live_chose_zero = False
    candidate_count = min(len(filtered_results), primary_limit)
    resources = {}
    current_corpus = None
    model_rows = []
    work_estimate = {}
    try:
        from context_learning.corpus import snapshot as corpus_snapshot
        from context_learning.live_select import choose
        from context_learning.seam import _private_state, _snapshot
        from context_learning import selection_port
        state = _private_state(config, card_roots[0])
        shared_state = None
        if shared_trial:
            from context_learning.shared import _current_config, _policy, _safe_root
            shared_state = _safe_root(config, card_roots[0])
            if shared_state is not None:
                _policy(shared_state)
                if shared_profile:
                    from context_learning.learning_profile import selection_root
                    shared_state=selection_root(config,card_roots[0])
                elif _current_config(shared_state, card_roots[0]) is None:shared_state = None
        current_corpus = corpus_snapshot(card_roots[0], config, qmd_paths=qmd_paths)
        indexed_hashes = selection_port.indexed_revisions(current_corpus)
        if shared_trial and live_plan['requested'] == 0:
            live_reason = live_plan['reason']
        elif shared_trial and shared_state is None:
            live_reason = 'shared_state_unavailable'
        elif state is None and shared_state is None:
            live_reason = 'private_state_unavailable'
        elif (pool_reason := selection_port.pool_rejection(
                filtered_results, primary_limit, is_wiki)) is not None:
            live_reason = pool_reason
        else:
            model_rows = selection_port.candidate_rows(
                filtered_results, primary_limit,
                resolve_path=resolve, snapshot=_snapshot, indexed=indexed_hashes)
            if shared_trial:
                from context_learning.adaptive_pool import actual_work
                from context_learning.compact_input import derive
                bodies = [derive(row['source_text'], row['revision_sha256'])['body_text']
                          for row in model_rows]
                work_estimate = actual_work(prompt, bodies,
                    edit_view=internal_edit,
                    rerank_target_seconds=shared_options.get('rerankTargetSeconds', 1.0))
                if not work_estimate['all_pairs_fit_estimate']:
                    raise ValueError('input_window_unavailable')
                if (internal_edit and
                        not work_estimate['within_rerank_target_estimate']):
                    raise ValueError('responsiveness_deferred')
            selection_kwargs = {
                'corpus_fingerprint': current_corpus['fingerprint'] if current_corpus else None,
                'deadline': deadline()}
            if shared_state is not None:
                if shared_profile:
                    from context_learning.candidate_apply import readable
                    readable(shared_state)
                else:
                    from context_learning.shared import trial_eligible
                    if not trial_eligible(shared_state, deadline=deadline()):raise ValueError('shared_trial_not_eligible')
                    selection_kwargs['pointer_name'] = 'experimental-checkpoint.json'
            selected_ids, live_reason, resources = choose(
                shared_state if shared_state is not None else state,
                prompt, model_rows, **selection_kwargs)
            if selected_ids is not None:
                final_results = selection_port.apply_selection(
                    filtered_results, primary_limit, selected_ids)
                live_chose_zero = not selected_ids
    except (OSError, ValueError, UnicodeError, KeyError, TypeError) as exc:
        live_reason = (str(exc) if str(exc) in
                       ('input_window_unavailable', 'responsiveness_deferred')
                       else 'candidate_unavailable')
    if shared_trial and live_reason != 'selected':
        final_results = filtered_results[:top_n]
    return {'final_results': final_results, 'reason': live_reason,
            'chose_zero': live_chose_zero, 'candidate_count': candidate_count,
            'resources': resources, 'corpus': current_corpus,
            'model_rows': model_rows, 'work_estimate': work_estimate}


class Stubs:
    """Patches learning module attributes, the way recall's e2e fixtures do."""

    def __init__(self, cards, **over):
        self.calls = []
        self.cfg = {
            'state': 'private-state', 'safe_root': 'shared-state', 'current_config': {},
            'selection_root': 'profile-state',
            'corpus': {'fingerprint': 'f' * 64, 'material': {'activeDocuments': [
                [c, r, h] for (c, r), h in cards.hashes.items()]}},
            'choose': lambda *a, **k: (['wiki/b.md', 'wiki/a.md'], 'selected', {'elapsed_seconds': .01}),
            'work': {'all_pairs_fit_estimate': True, 'within_rerank_target_estimate': True,
                     'input_tokens_estimate': 7},
            'eligible': True, 'readable': None}
        self.cfg.update(over)

    def stub(self, name, value):
        def call(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            result = value(*args, **kwargs) if callable(value) else value
            if isinstance(result, BaseException):
                raise result
            return result
        return call

    def names(self):
        return [c[0] for c in self.calls]

    def __enter__(self):
        self.stack = ExitStack()
        c = self.cfg
        for module, name, value in (
                (seam, '_private_state', c['state']),
                (shared, '_safe_root', c['safe_root']),
                (shared, '_policy', None),
                (shared, '_current_config', c['current_config']),
                (shared, 'trial_eligible', c['eligible']),
                (learning_profile, 'selection_root', c['selection_root']),
                (corpus, 'snapshot', c['corpus']),
                (live_select, 'choose', c['choose']),
                (adaptive_pool, 'actual_work', c['work']),
                (compact_input, 'derive', lambda text, sha: {'body_text': text}),
                (candidate_apply, 'readable', c['readable'])):
            self.stack.enter_context(mock.patch.object(module, name, self.stub(name, value)))
        return self

    def __exit__(self, *exc):
        self.stack.close()


class SelectionReadTests(unittest.TestCase):
    def setUp(self):
        self.cards = Cards()
        for name in ('a.md', 'b.md', 'c.md', 'd.md', 'e.md'):
            self.cards.add(name, f'---\ntitle: {name}\n---\nbody {name}\n'.encode())
        self.pool = [{'file': f'qmd://wiki/{n}.md', '_collection': 'wiki'} for n in 'abcde']
        self.config = {'contextLearning': {'liveSelection': True}}
        self.qmd_paths = {'selected': False}

    def tearDown(self):
        self.cards.close()

    def call(self, fn, pool, limit, top_n, trial, profile, plan, edit):
        common = dict(config=self.config, qmd_paths=self.qmd_paths, prompt='synthetic prompt',
                      shared_trial=trial, shared_profile=profile, live_plan=plan,
                      deadline=lambda: DEADLINE)
        is_wiki = lambda collection: collection == 'wiki'
        if fn is legacy_select:
            return legacy_select(pool, limit, top_n, card_roots=(self.cards.root, None),
                                 is_wiki=is_wiki, resolve=self.cards.resolve,
                                 internal_edit=edit, shared_options={'rerankTargetSeconds': 2.0},
                                 **common)
        return R.select(pool, limit, top_n, card_root=self.cards.root,
                        is_wiki_collection=is_wiki, resolve_path=self.cards.resolve,
                        edit_view=edit, rerank_target_seconds=2.0, **common)

    def run_both(self, pool=None, limit=15, top_n=3, trial=False, profile=False,
                 plan=None, edit=False, **stubs):
        """Same outcome, same raised error and same learning-call order as before."""
        pool = self.pool if pool is None else pool
        runs = []
        for fn in (legacy_select, R.select):
            with Stubs(self.cards, **stubs) as s:
                try:
                    out = ('ok', self.call(fn, pool, limit, top_n, trial, profile, plan, edit))
                except (Exception, hook_budget.HookDeadlineExceeded) as exc:
                    out = ('raise', type(exc).__name__, str(exc))
                runs.append((out, s.calls))
        self.assertEqual(runs[0], runs[1])
        self.last_names = [c[0] for c in runs[1][1]]
        self.last_calls = runs[1][1]
        return runs[1][0][1] if runs[1][0][0] == 'ok' else runs[1][0]

    def files(self, hits):
        return [h['file'].rsplit('/', 1)[-1] for h in hits]

    def test_selected_keeps_selector_order_and_kwargs(self):
        out = self.run_both()
        self.assertEqual(out['reason'], 'selected')
        self.assertEqual(self.files(out['final_results']), ['b.md', 'a.md'])
        self.assertIs(out['final_results'][0], self.pool[1])
        self.assertEqual(len(out['model_rows']), 5)
        self.assertEqual(out['resources'], {'elapsed_seconds': .01})
        name, args, kwargs = self.last_calls[-1]
        self.assertEqual((name, args[0]), ('choose', 'private-state'))
        self.assertEqual(kwargs, {'corpus_fingerprint': 'f' * 64, 'deadline': DEADLINE})
        # Non-trial never touches shared/profile consent.
        self.assertEqual(self.last_names, ['_private_state', 'snapshot', 'choose'])

    def test_default_top3_vs_trial_top_n_fallback(self):
        timeout = lambda *a, **k: (None, 'timeout', {})
        out = self.run_both(top_n=5, choose=timeout)
        self.assertEqual((out['reason'], len(out['final_results'])), ('timeout', 3))
        out = self.run_both(top_n=5, trial=True, plan={'requested': 15, 'reason': 'admitted'},
                            choose=timeout)
        self.assertEqual((out['reason'], len(out['final_results'])), ('timeout', 5))
        out = self.run_both(top_n=2, choose=timeout)
        self.assertEqual(len(out['final_results']), 2)

    def test_semantic_zero(self):
        out = self.run_both(choose=lambda *a, **k: ([], 'selected', {}))
        self.assertEqual((out['final_results'], out['chose_zero']), ([], True))

    def test_pool_and_state_reasons(self):
        out = self.run_both(state=None)
        self.assertEqual(out['reason'], 'private_state_unavailable')
        self.assertIsNotNone(out['corpus'])  # snapshot taken before the check
        self.assertNotIn('choose', self.last_names)
        out = self.run_both(pool=[])
        self.assertEqual((out['reason'], out['candidate_count']), ('no_candidates', 0))
        out = self.run_both(pool=self.pool + [{'file': 'raw/x.md', '_collection': 'raw'}])
        self.assertEqual(out['reason'], 'non_wiki_pool')
        self.assertEqual(self.run_both(pool=self.pool + [{'file': 'raw/x.md', '_collection': 'raw'}],
                                       limit=5)['reason'], 'selected')
        self.assertEqual(self.run_both(limit=2)['candidate_count'], 2)

    def test_stale_or_missing_corpus_is_candidate_unavailable(self):
        stale = {'fingerprint': 'f' * 64, 'material': {'activeDocuments': [['wiki', 'a.md', 'old']]}}
        out = self.run_both(corpus=stale)
        self.assertEqual((out['reason'], out['model_rows']), ('candidate_unavailable', []))
        self.assertIs(out['corpus']['material'], stale['material'])  # partial value kept
        self.assertEqual(self.files(out['final_results']), ['a.md', 'b.md', 'c.md'])
        out = self.run_both(corpus=None)
        self.assertEqual((out['reason'], out['corpus']), ('candidate_unavailable', None))
        out = self.run_both(corpus=OSError('unreadable index'))
        self.assertEqual((out['reason'], out['corpus']), ('candidate_unavailable', None))

    def test_corpus_receives_original_qmd_paths(self):
        self.run_both()
        snapshot = next(c for c in self.last_calls if c[0] == 'snapshot')
        self.assertIs(snapshot[2]['qmd_paths'], self.qmd_paths)
        self.assertEqual(snapshot[1], (self.cards.root, self.config))

    def test_trial_admission_and_consent(self):
        plan = {'requested': 15, 'reason': 'admitted'}
        out = self.run_both(trial=True, plan={'requested': 0, 'reason': 'deadline_short'}, top_n=4)
        self.assertEqual((out['reason'], len(out['final_results'])), ('deadline_short', 4))
        self.assertIn('snapshot', self.last_names)  # check order: corpus before plan
        out = self.run_both(trial=True, plan=plan, safe_root=None)
        self.assertEqual(out['reason'], 'shared_state_unavailable')
        self.assertNotIn('_policy', self.last_names)
        out = self.run_both(trial=True, plan=plan, current_config=None)
        self.assertEqual(out['reason'], 'shared_state_unavailable')  # private state not used
        out = self.run_both(trial=True, plan=plan, eligible=False)
        self.assertEqual(out['reason'], 'candidate_unavailable')
        self.assertNotIn('choose', self.last_names)
        out = self.run_both(trial=True, plan=plan)
        self.assertEqual(out['reason'], 'selected')
        self.assertEqual(self.last_names, ['_private_state', '_safe_root', '_policy', '_current_config',
                                           'snapshot', 'derive', 'derive', 'derive', 'derive', 'derive',
                                           'actual_work', 'trial_eligible', 'choose'])
        eligible = next(c for c in self.last_calls if c[0] == 'trial_eligible')
        self.assertEqual(eligible[1:], (('shared-state',), {'deadline': DEADLINE}))
        name, args, kwargs = self.last_calls[-1]
        self.assertEqual(args[0], 'shared-state')
        self.assertEqual(kwargs['pointer_name'], 'experimental-checkpoint.json')
        work = next(c for c in self.last_calls if c[0] == 'actual_work')
        self.assertEqual(work[2], {'edit_view': False, 'rerank_target_seconds': 2.0})

    def test_profile_consent_skips_trial_grant(self):
        plan = {'requested': 15, 'reason': 'admitted'}
        out = self.run_both(trial=True, profile=True, plan=plan)
        self.assertEqual(out['reason'], 'selected')
        self.assertNotIn('_current_config', self.last_names)
        self.assertNotIn('trial_eligible', self.last_names)
        self.assertIn('readable', self.last_names)
        name, args, kwargs = self.last_calls[-1]
        self.assertEqual(args[0], 'profile-state')
        self.assertNotIn('pointer_name', kwargs)
        out = self.run_both(trial=True, profile=True, plan=plan, readable=OSError('unreadable'))
        self.assertEqual((out['reason'], len(out['final_results'])), ('candidate_unavailable', 3))
        out = self.run_both(trial=True, profile=True, plan=plan, selection_root=None)
        self.assertEqual(out['reason'], 'shared_state_unavailable')

    def test_work_estimate_reasons_keep_partial_values(self):
        plan = {'requested': 15, 'reason': 'admitted'}
        too_big = {'all_pairs_fit_estimate': False, 'within_rerank_target_estimate': True,
                   'input_tokens_estimate': 99}
        out = self.run_both(trial=True, plan=plan, work=too_big)
        self.assertEqual(out['reason'], 'input_window_unavailable')
        self.assertEqual(out['work_estimate'], too_big)
        self.assertEqual(len(out['model_rows']), 5)
        slow = {'all_pairs_fit_estimate': True, 'within_rerank_target_estimate': False}
        self.assertEqual(self.run_both(trial=True, plan=plan, work=slow, edit=True)['reason'],
                         'responsiveness_deferred')
        self.assertEqual(self.run_both(trial=True, plan=plan, work=slow)['reason'], 'selected')

    def test_selector_failures(self):
        out = self.run_both(choose=lambda *a, **k: (['wiki/zzz.md'], 'selected', {'pairs': 5}))
        self.assertEqual(out['reason'], 'candidate_unavailable')
        self.assertEqual(out['resources'], {'pairs': 5})  # reached before the KeyError
        self.assertEqual(self.files(out['final_results']), ['a.md', 'b.md', 'c.md'])
        self.assertFalse(out['chose_zero'])
        for bad in ((None, 'selected'), None):
            out = self.run_both(choose=lambda *a, bad=bad, **k: bad)
            self.assertEqual((out['reason'], out['resources']), ('candidate_unavailable', {}))
        for exc in (UnicodeError('x'), TypeError('x'), KeyError('x')):
            self.assertEqual(self.run_both(choose=exc)['reason'], 'candidate_unavailable')

    def test_uncaught_errors_propagate(self):
        out = self.run_both(choose=hook_budget.HookDeadlineExceeded('budget'))
        self.assertEqual(out, ('raise', 'HookDeadlineExceeded', 'budget'))
        out = self.run_both(choose=RuntimeError('boom'))
        self.assertEqual(out, ('raise', 'RuntimeError', 'boom'))
        out = self.run_both(trial=True, plan={'requested': 15, 'reason': 'admitted'},
                            eligible=hook_budget.HookDeadlineExceeded('grant'))
        self.assertEqual(out[:2], ('raise', 'HookDeadlineExceeded'))

    def test_caught_tuple_and_call_time_lookup(self):
        self.assertEqual(R.CAUGHT, (OSError, ValueError, UnicodeError, KeyError, TypeError))
        # Learning names are looked up per call, so attribute patches apply.
        tree = ast.parse(Path('core/context_learning/selection_read.py').read_text(encoding='utf8'))
        top = {getattr(n, 'module', None) or n.names[0].name
               for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))}
        self.assertEqual(top, {'__future__'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
