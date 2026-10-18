"""Same-session freshness and worktree path switch (stage A), synthetic only.

recall runs in QMD_QUERY_FIXTURE mode and verifies each returned hit against a
synthetic SQLite index named by QMD_QUERY_FIXTURE_INDEX. No QMD binary, model,
daemon, Git command, network, or operating QMD DB is used, and the synthetic
index is only read. Neither checkout has a SessionStart snapshot and both set
sessionStartSync:false, so fresh raw hits must still be injected.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

repository = Path.cwd()
sys.path.insert(0, str(repository / 'core'))
import qmd_scope

PROMPT = 'Which direction does the amber compass point in the synthetic guide?'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def make_index(path, rows, *, with_hash=True):
    with sqlite3.connect(path) as db:
        if with_hash:
            db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
            db.executemany('INSERT INTO documents VALUES (?,?,?,1)', rows)
        else:
            db.execute('CREATE TABLE documents(collection TEXT,path TEXT,active INTEGER)')
            db.executemany('INSERT INTO documents VALUES (?,?,1)',
                           [(collection, relative) for collection, relative, _ in rows])
    db.close()


# Projects must not live under /tmp or /private (resolve_paths refuses them).
with tempfile.TemporaryDirectory(prefix='qmd-same-session-', dir=Path.home()) as name:
    base = Path(name).resolve()
    home = base / 'home'
    checkout_a = home / 'work/checkout-a'
    checkout_b = home / 'work/checkout-b'
    settings = {'indexing': True, 'collections': ['notes'],
                'collectionPaths': {'notes': 'docs'}, 'collectionRoles': {'notes': 'raw'},
                'recallStrategy': 'flat', 'topN': 5, 'minScore': 0,
                'sessionStartSync': False,
                'events': ['sessionStart', 'userPromptSubmit', 'postToolUse']}
    for checkout in (checkout_a, checkout_b):
        (checkout / 'docs').mkdir(parents=True)
        (checkout / '.auto-context/wiki').mkdir(parents=True)
        (checkout / '.auto-context/settings.json').write_text(json.dumps(settings))

    alpha = b'Alpha guide: the amber compass points north.\n'
    beta = b'Beta guide: a second amber compass note.\n'
    gamma = b'Gamma guide: only checkout A has this amber compass page.\n'
    (checkout_a / 'docs/alpha.md').write_bytes(alpha)
    (checkout_a / 'docs/beta.md').write_bytes(beta)
    (checkout_a / 'docs/gamma.md').write_bytes(gamma)
    # Checkout B: alpha differs, beta has identical bytes, gamma is absent.
    (checkout_b / 'docs/alpha.md').write_bytes(b'Alpha guide on branch B: it points south.\n')
    (checkout_b / 'docs/beta.md').write_bytes(beta)

    delta = b'Delta guide: an untracked amber compass page, already indexed.\n'
    outside = base / 'outside.md'
    outside.write_bytes(b'Outside secret amber compass text.\n')
    index = base / 'synthetic-index.sqlite'
    make_index(index, [('notes', 'alpha.md', sha(alpha)), ('notes', 'beta.md', sha(beta)),
                       ('notes', 'gamma.md', sha(gamma)), ('notes', 'delta.md', sha(delta)),
                       ('notes', 'link.md', sha(outside.read_bytes()))])
    legacy_index = base / 'no-hash-index.sqlite'
    make_index(legacy_index, [('notes', 'alpha.md', sha(alpha))], with_hash=False)
    index_bytes = index.read_bytes()

    fixture = base / 'query.json'
    log = base / 'recall.jsonl'
    environment = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '1',
                   'QMD_QUERY_FIXTURE': str(fixture), 'QMD_RECALL_LOG': str(log),
                   'INDEX_PATH': str(base / 'operating-index-never-opened.sqlite'),
                   'XDG_CACHE_HOME': str(base / 'cache'),
                   'QMD_CONFIG_DIR': str(base / 'qmd-config'),
                   'QMD_DAEMON_URL': 'http://127.0.0.1:1',
                   'PYTHONDONTWRITEBYTECODE': '1'}
    for key in ('QMD_QUERY_FIXTURE_RAW', 'QMD_QUERY_FIXTURE_LEX', 'QMD_SHADOW_QUERY',
                'QMD_EDIT_INTERNAL', 'QMD_DEFERRED_CAPTURE_REPLAY', 'QMD_SANDBOX'):
        environment.pop(key, None)

    def hit(relative, score, collection='notes'):
        return {'file': f'qmd://{collection}/{relative}', 'score': score,
                'title': 'Untrusted QMD title', 'snippet': 'untrusted snippet'}

    ALL = [hit('alpha.md', .9), hit('beta.md', .8), hit('gamma.md', .7)]

    def recall(cwd, results, *, fixture_index=index):
        fixture.write_text(json.dumps({'results': results}))
        log.write_text('')
        env = dict(environment)
        if fixture_index is None:
            env.pop('QMD_QUERY_FIXTURE_INDEX', None)
        else:
            env['QMD_QUERY_FIXTURE_INDEX'] = str(fixture_index)
        docs_before = sorted(p.name for p in (cwd / 'docs').iterdir())
        completed = subprocess.run([sys.executable, str(repository / 'core/recall.py')],
            input=json.dumps({'prompt': PROMPT, 'cwd': str(cwd),
                              'hook_event_name': 'UserPromptSubmit'}),
            text=True, capture_output=True, env=env, timeout=30, check=False)
        assert completed.returncode == 0, completed.stderr
        # Recall is read-only: the index bytes and checkout file set are unchanged.
        assert index.read_bytes() == index_bytes
        assert sorted(p.name for p in (cwd / 'docs').iterdir()) == docs_before
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        selection = [entry for entry in entries if 'index_guard' in entry]
        assert len(selection) == 1, entries
        context = ''
        if completed.stdout.strip():
            context = json.loads(completed.stdout)['hookSpecificOutput']['additionalContext']
        return context, selection[0]

    # 1. Fresh hits in checkout A are injected; QMD's title/snippet are not.
    context, entry = recall(checkout_a, ALL)
    assert entry['index_guard'] == 'fixture' and entry['index_guard_checked'] == 3, entry
    assert entry['index_guard_dropped'] == 0 and entry['project_root'] == str(checkout_a), entry
    for stem in ('alpha', 'beta', 'gamma'):
        assert f'{stem}.md - {stem}' in context, context
    assert 'Untrusted QMD title' not in context and 'untrusted snippet' not in context
    fresh = True

    # 2. Same-session modification: only the edited hit drops; others survive.
    (checkout_a / 'docs/alpha.md').write_bytes(b'Alpha guide edited mid-session.\n')
    context, entry = recall(checkout_a, ALL)
    assert entry['index_guard_drop_reasons'] == {'modified': 1}, entry
    assert 'alpha.md' not in context and 'beta.md - beta' in context, context
    assert 'gamma.md - gamma' in context, context
    modified_dropped = True

    # 3. Branch return to identical bytes (also detached HEAD: no Git lookup)
    # makes the indexed revision current again.
    (checkout_a / 'docs/alpha.md').write_bytes(alpha)
    context, entry = recall(checkout_a, ALL)
    assert entry['index_guard_dropped'] == 0 and 'alpha.md - alpha' in context, (entry, context)
    same_content_return = True

    # 4. Deletion drops only the deleted hit.
    (checkout_a / 'docs/gamma.md').unlink()
    context, entry = recall(checkout_a, ALL)
    assert entry['index_guard_drop_reasons'] == {'missing': 1}, entry
    assert 'gamma.md' not in context and 'alpha.md - alpha' in context, context
    (checkout_a / 'docs/gamma.md').write_bytes(gamma)
    deleted_dropped = True

    # 5. Untracked files: an indexed one is current; a new file is unavailable
    # until an index update (absent from results, or returned without a row).
    (checkout_a / 'docs/delta.md').write_bytes(delta)
    (checkout_a / 'docs/epsilon.md').write_bytes(b'Epsilon amber compass page, never indexed.\n')
    (checkout_a / 'docs/zeta.md').write_bytes(b'Zeta amber compass page, never indexed.\n')
    context, entry = recall(checkout_a, [hit('delta.md', .9), hit('zeta.md', .8)])
    assert entry['index_guard_drop_reasons'] == {'not_indexed': 1}, entry
    assert 'delta.md - delta' in context and 'zeta' not in context, context
    assert 'epsilon' not in context
    untracked_handled = True

    # 6. Out-of-scope and malformed rows never pass, and none are injected.
    (checkout_a / 'docs/link.md').symlink_to(outside)
    (checkout_a / 'docs/link-inside.md').symlink_to(checkout_a / 'docs/beta.md')
    context, entry = recall(checkout_a, [
        hit('../.auto-context/settings.json', .9), hit('alpha.md', .9, 'other'),
        hit('link.md', .9), hit('link-inside.md', .9),
        {'file': 'qmd://notes/beta.md', 'score': 'nan'},
        {'file': 'qmd://notes/beta.md', 'score': 1, '_collection': 'other'}, 'not-a-row'])
    assert context == '' and entry['reason'] == 'no_results_after_filter', (context, entry)
    assert entry['index_guard_drop_reasons'] == {
        'unsafe_path': 1, 'foreign_collection': 2, 'out_of_scope': 2, 'malformed': 2}, entry
    out_of_scope_dropped = True

    # 7. Worktree switch: the payload cwd selects checkout B. A's differing and
    # A-only paths are dropped; identical bytes are B's own file and allowed.
    context, entry = recall(checkout_b, ALL)
    assert entry['project_root'] == str(checkout_b), entry
    assert entry['index_guard_drop_reasons'] == {'modified': 1, 'missing': 1}, entry
    assert 'beta.md - beta' in context and 'alpha' not in context and 'gamma' not in context
    assert 'north' not in context and 'only checkout A' not in context
    context, entry = recall(checkout_a, ALL)
    assert entry['project_root'] == str(checkout_a) and entry['index_guard_dropped'] == 0
    assert 'alpha.md - alpha' in context and 'gamma.md - gamma' in context
    worktree_switch_isolated = True

    # 8. An index that cannot prove a revision fails closed for every hit.
    context, entry = recall(checkout_a, ALL, fixture_index=base / 'absent.sqlite')
    assert context == '' and entry['index_guard_drop_reasons'] == {'index_unavailable': 3}
    context, entry = recall(checkout_a, ALL, fixture_index=legacy_index)
    assert context == '' and entry['index_guard_drop_reasons'] == {'index_unavailable': 3}
    unavailable_fails_closed = True

    # 9. A fixture without an index keeps the pre-existing unguarded fixture
    # route, and says so in the log instead of implying freshness.
    context, entry = recall(checkout_a, ALL, fixture_index=None)
    assert entry['index_guard'] == 'none' and entry['index_guard_checked'] == 0, entry
    assert 'alpha.md - Untrusted QMD title' in context, context
    unguarded_fixture_labelled = True

    # 10. Direct bounds and scope accounting.
    drops = {}
    assert qmd_scope.scoped_hits([None] * (qmd_scope.MAX_CHECKED_ROWS + 3), checkout_a,
                                 settings, {'notes'}, index, drops=drops) == []
    assert drops == {'row_limit': 3, 'malformed': qmd_scope.MAX_CHECKED_ROWS}, drops
    drops = {}
    assert qmd_scope.scoped_hits(ALL, base / 'absent-checkout', settings, {'notes'},
                                 index, drops=drops) == []
    assert drops == {'scope_unavailable': 3}, drops
    drops = {}
    assert qmd_scope.scoped_hits(ALL, checkout_a, {**settings, 'indexing': False},
                                 {'notes'}, index, drops=drops) == []
    assert drops == {'scope_refused': 3}, drops
    # Positional callers without `drops` keep the original contract.
    kept = qmd_scope.scoped_hits(ALL, checkout_a, settings, {'notes'}, index)
    assert [row['file'] for row in kept] == [row['file'] for row in ALL]
    assert index.read_bytes() == index_bytes
    bounded_accounting = True

print(json.dumps({'fresh': fresh, 'modifiedDropped': modified_dropped,
                  'sameContentReturn': same_content_return,
                  'deletedDropped': deleted_dropped, 'untrackedHandled': untracked_handled,
                  'outOfScopeDropped': out_of_scope_dropped,
                  'worktreeSwitchIsolated': worktree_switch_isolated,
                  'unavailableFailsClosed': unavailable_fails_closed,
                  'unguardedFixtureLabelled': unguarded_fixture_labelled,
                  'boundedAccounting': bounded_accounting}))
