"""Local-only synthetic SessionStart CUD, shared references, and crash replay."""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd() / 'core'))
from context_learning.shared import _db
from context_learning.synthetic_source_reconcile import _detect, _files, _reconcile_authorized
from context_learning.synthetic_scope import _generated_collections_only
import wiki_freshness
import yaml_scalars


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


with tempfile.TemporaryDirectory(prefix='qmd-synthetic-reconcile-', dir='/tmp') as temp:
    home = Path(temp)
    project = home / 'novel'
    story = project / '마지막 마법의 도서관'
    raw_dir = story / '04_Manuscript'
    wiki = story / '.auto-context/wiki'
    root = project / '.qmd-fantasy-shared-20261008'
    raw_dir.mkdir(parents=True)
    wiki.mkdir(parents=True)
    (project / '.auto-context/wiki').mkdir(parents=True)
    root.mkdir(mode=0o700)
    a = raw_dir / 'a.md'
    b = raw_dir / 'b.md'
    a.write_text('Synthetic source A.\n')
    b.write_text('Synthetic source B.\n')

    def revision(source):
        snap = wiki_freshness.snapshot_file(source)
        return {'kind': 'file', 'path': f'{story.name}/04_Manuscript/{source.name}',
                'collection': 'fantasy-20261008-manuscript', **snap}

    refs = [revision(a), revision(b)]
    def card(name, rows):
        page = wiki / name
        proof = yaml_scalars.dump_source_revisions(rows)
        page.write_text('\n'.join(['---', 'title: Synthetic shared card',
            'status: verified', 'createdBy: qmd-auto-context',
            'sourceRevisions:', *('  - ' + item for item in proof),
            '---', 'Synthetic fact shared across sources.', '']))
        return page
    shared = card('shared.md', refs)
    b_only = card('b-only.md', [refs[1]])
    shared_sha = sha(shared.read_bytes())
    with _db(root) as db:
        for relative, source in [('04_Manuscript/a.md', a),
                                 ('04_Manuscript/b.md', b),
                                 ('.auto-context/wiki/shared.md', shared),
                                 ('.auto-context/wiki/b-only.md', b_only)]:
            db.execute('INSERT INTO experiment_generated '
                       '(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) '
                       'VALUES (?,?,?,?,?,?,?)',
                       ('writing' if source in (a, b) else 'wiki_review', 1, 1,
                        relative, sha(source.read_bytes()), None, time.time()))
    enqueued = []
    with patch('context_learning.synthetic_source_reconcile._request_index',
               side_effect=lambda project: enqueued.append(str(project))):
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        a.unlink()  # shell deletion: no edit hook receipt
        with patch('context_learning.synthetic_source_reconcile.os.replace',
                   side_effect=OSError('synthetic crash before archive')):
            try:
                _reconcile_authorized(root, project, story)
            except OSError as exc:
                assert 'synthetic crash' in str(exc)
            else:
                raise AssertionError('crash did not interrupt archive')
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM synthetic_source_events "
                              "WHERE operation='delete' AND state='pending'").fetchone()[0] == 1
            assert db.execute("SELECT COUNT(*) FROM synthetic_retired_cards "
                              "WHERE state='pending'").fetchone()[0] == 1
        assert shared.exists() and b_only.exists()
        recovered = _reconcile_authorized(root, project, story)
        assert recovered['detected'] == 0 and recovered['wikiCollectionsQueued'] == 1
        assert not shared.exists() and b_only.exists() and b.exists()
        _generated_collections_only(root, project)
        with _db(root) as db:
            row = db.execute('SELECT archive_relative,source_refs,state FROM '
                             'synthetic_retired_cards WHERE relative_path=?',
                             ('.auto-context/wiki/shared.md',)).fetchone()
            assert row and row[2] == 'queued' and len(json.loads(row[1])) == 2
            archive = root / row[0]
            assert archive.is_file() and sha(archive.read_bytes()) == shared_sha
            assert db.execute("SELECT COUNT(*) FROM synthetic_source_events "
                              "WHERE operation='delete' AND state='retired'").fetchone()[0] == 1
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        assert len(enqueued) == 1
        # An external update of the still-known source does not prove the new
        # bytes' generation origin, so the job stays local and reviewable.
        b.write_text('Synthetic source B revised by shell.\n')
        updated = _reconcile_authorized(root, project, story)
        assert updated['detected'] == 1 and updated['externalCalls'] == 0
        assert not b_only.exists()
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM synthetic_source_events "
                              "WHERE operation='update' AND state='needs_origin'").fetchone()[0] == 1
        new = raw_dir / 'new.md'
        new.write_text('Unknown origin file in the approved story.\n')
        created = _reconcile_authorized(root, project, story)
        assert created['detected'] == 1 and created['externalCalls'] == 0
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM synthetic_source_events "
                              "WHERE operation='create' AND state='needs_origin'").fetchone()[0] == 1
        b.write_text('Synthetic source B.\n')
        assert _reconcile_authorized(root, project, story)['wikiCardsRestored'] == 1
        assert b_only.exists() and not shared.exists()
        a.write_text('Synthetic source A.\n')
        assert _reconcile_authorized(root, project, story)['wikiCardsRestored'] == 1
        assert shared.exists() and sha(shared.read_bytes()) == shared_sha
        assert archive.is_file() and sha(archive.read_bytes()) == shared_sha

with tempfile.TemporaryDirectory(prefix='qmd-synthetic-reconcile-chain-', dir='/tmp') as temp:
    project = Path(temp) / 'novel'
    story = project / '마지막 마법의 도서관'
    raw_dir = story / '04_Manuscript'
    wiki = story / '.auto-context/wiki'
    root = project / '.qmd-fantasy-shared-20261008'
    raw_dir.mkdir(parents=True)
    wiki.mkdir(parents=True)
    root.mkdir(mode=0o700)
    source = raw_dir / 'ep001.md'
    source.write_text('Revision A\n')
    snap = wiki_freshness.snapshot_file(source)
    refs = [{'kind': 'file', 'path': f'{story.name}/04_Manuscript/ep001.md',
             'collection': 'fantasy-20261008-manuscript', **snap}]
    proof = yaml_scalars.dump_source_revisions(refs)
    content = '\n'.join(['---', 'title: Chain', 'status: verified',
                         'createdBy: qmd-auto-context', 'sourceRevisions:',
                         *('  - ' + item for item in proof), '---', 'Fact A.', ''])
    user_card = wiki / '00-user-edited.md'
    user_card.write_text(content + '\nUser-edited.\n')
    generated_card = wiki / '10-generated.md'
    generated_card.write_text(content)
    with _db(root) as db:
        for kind, relative, path in [('writing', '04_Manuscript/ep001.md', source),
                                     ('wiki_review', '.auto-context/wiki/10-generated.md', generated_card)]:
            db.execute('INSERT INTO experiment_generated '
                       '(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (kind, 1, 1, relative, sha(path.read_bytes()), None, time.time()))
    with patch('context_learning.synthetic_source_reconcile._request_index') as enqueue:
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        source.write_text('Revision B\n')
        # Detection commits, then the process crashes before event replay.
        with _db(root) as db:
            assert _detect(db, story, _files(story)) == 1
        source.write_text('Revision C\n')
        recovered = _reconcile_authorized(root, project, story)
        assert recovered['detected'] == 1 and recovered['wikiCollectionsQueued'] == 1
        assert user_card.exists() and not generated_card.exists()
        assert enqueue.call_count == 1
        with _db(root) as db:
            states = db.execute('SELECT operation,state FROM synthetic_source_events '
                                'ORDER BY recorded_at,event_id').fetchall()
            assert ('update', 'needs_review') in states
            assert ('update', 'needs_origin') in states
            retired = db.execute('SELECT state FROM synthetic_retired_cards '
                                 'WHERE relative_path=?',
                                 ('.auto-context/wiki/10-generated.md',)).fetchone()
            assert retired == ('queued',)
        source.write_text('Revision A\n')
        real_link = os.link
        def crash_after_link(source_path, target_path):
            real_link(source_path, target_path)
            raise OSError('synthetic crash after restore link')
        with patch('context_learning.synthetic_source_reconcile.os.link',
                   side_effect=crash_after_link):
            try:
                _reconcile_authorized(root, project, story)
            except OSError as exc:
                assert 'synthetic crash after restore link' in str(exc)
            else:
                raise AssertionError('restore crash did not occur')
        restore_temp = next(wiki.glob('.qmd-restore-*.tmp'))
        assert generated_card.is_file() and restore_temp.stat().st_nlink == 2
        with patch('context_learning.synthetic_source_reconcile._request_index',
                   side_effect=OSError('synthetic crash before restore queue')):
            try:
                _reconcile_authorized(root, project, story)
            except OSError as exc:
                assert 'synthetic crash before restore queue' in str(exc)
            else:
                raise AssertionError('restore queue crash did not occur')
        with _db(root) as db:
            assert db.execute('SELECT state FROM synthetic_retired_cards '
                              'WHERE relative_path=?',
                              ('.auto-context/wiki/10-generated.md',)).fetchone() == (
                                  'restore_pending_index',)
        source.write_text('Revision D\n')
        reedited = _reconcile_authorized(root, project, story)
        assert reedited['wikiCollectionsQueued'] == 1
        assert not generated_card.exists()
        with _db(root) as db:
            assert db.execute('SELECT state FROM synthetic_retired_cards '
                              'WHERE relative_path=?',
                              ('.auto-context/wiki/10-generated.md',)).fetchone() == ('queued',)
        source.write_text('Revision A\n')
        restored_result = _reconcile_authorized(root, project, story)
        assert restored_result['wikiCardsRestored'] == 1
        assert generated_card.is_file() and generated_card.read_text() == content
        assert not restore_temp.exists() and generated_card.stat().st_nlink == 1
        with _db(root) as db:
            archive_rel = db.execute('SELECT archive_relative FROM synthetic_retired_cards '
                                     'WHERE relative_path=? AND state=?',
                                     ('.auto-context/wiki/10-generated.md', 'restored')).fetchone()[0]
            assert (root / archive_rel).read_text() == content
        source.write_text('Revision E\n')
        re_retired = _reconcile_authorized(root, project, story)
        assert re_retired['wikiCollectionsQueued'] == 1
        assert not generated_card.exists() and (root / archive_rel).read_text() == content
        source.write_text('Revision A\n')
        with patch('context_learning.synthetic_source_reconcile.os.link',
                   side_effect=OSError('synthetic crash before restore link')):
            try:
                _reconcile_authorized(root, project, story)
            except OSError as exc:
                assert 'synthetic crash before restore link' in str(exc)
            else:
                raise AssertionError('pre-link crash did not occur')
        assert len(list(wiki.glob('.qmd-restore-*.tmp'))) == 1
        source.write_text('Revision F\n')
        _reconcile_authorized(root, project, story)
        assert not list(wiki.glob('.qmd-restore-*.tmp'))
    restored = raw_dir / 'ep002.md'
    restored.write_text('Revision A again\n')
    restored_sha = sha(restored.read_bytes())
    snap2 = wiki_freshness.snapshot_file(restored)
    refs2 = [{'kind': 'file', 'path': f'{story.name}/04_Manuscript/ep002.md',
              'collection': 'fantasy-20261008-manuscript', **snap2}]
    card2 = wiki / '20-still-current.md'
    card2.write_text('\n'.join(['---', 'title: Reverted', 'status: verified',
                                'createdBy: qmd-auto-context', 'sourceRevisions:',
                                *('  - ' + item for item in yaml_scalars.dump_source_revisions(refs2)),
                                '---', 'Fact restored.', '']))
    with _db(root) as db:
        for kind, relative, path in [('writing', '04_Manuscript/ep002.md', restored),
                                     ('wiki_review', '.auto-context/wiki/20-still-current.md', card2)]:
            db.execute('INSERT INTO experiment_generated '
                       '(kind,episode,slot,relative_path,sha256,prior_sha256,recorded_at) '
                       'VALUES (?,?,?,?,?,?,?)',
                       (kind, 2, 1, relative, sha(path.read_bytes()), None, time.time()))
    with patch('context_learning.synthetic_source_reconcile._request_index') as enqueue:
        assert _reconcile_authorized(root, project, story)['detected'] == 0
        restored.write_text('Revision B again\n')
        with _db(root) as db:
            assert _detect(db, story, _files(story)) == 1
        restored.write_text('Revision A again\n')
        assert sha(restored.read_bytes()) == restored_sha
        reverted = _reconcile_authorized(root, project, story)
        assert reverted['detected'] == 1 and reverted['wikiCollectionsQueued'] == 0
        assert card2.is_file() and enqueue.call_count == 0
        with _db(root) as db:
            assert db.execute("SELECT COUNT(*) FROM synthetic_source_events "
                              "WHERE relative_path='04_Manuscript/ep002.md' "
                              "AND state='superseded'").fetchone()[0] == 1
    generated_card.write_text(content + '\nUser edit at retired path.\n')
    source.write_text('Revision A\n')
    restored.write_text('Revision B again\n')
    with patch('context_learning.synthetic_source_reconcile._request_index') as enqueue:
        mixed = _reconcile_authorized(root, project, story)
        assert mixed['wikiCollectionsQueued'] == 1
        assert not card2.exists() and generated_card.read_text().endswith(
            'User edit at retired path.\n')
        assert enqueue.call_count == 1
        with _db(root) as db:
            assert db.execute('SELECT state FROM synthetic_retired_cards '
                              'WHERE relative_path=?',
                              ('.auto-context/wiki/20-still-current.md',)).fetchone() == ('queued',)
    print(json.dumps({'deleteRetiresExactCard': True,
                      'otherSharedSourcePreserved': True,
                      'multiSourceRestoreRequiresAllCurrent': True,
                      'crashReplay': True, 'repeatedStartIdempotent': True,
                      'updatesAndCreatesNeedOrigin': True,
                      'chainedRevisionReplayed': True,
                      'reviewOnlyCardDoesNotBlockGenerated': True,
                      'revertedCurrentCardPreserved': True,
                      'exactArchiveRestoreAndRetire': True,
                      'restoreQueueCrashThenReedit': True,
                      'abandonedRestoreTempCleaned': True,
                      'conflictingRestoreDoesNotStrandRetirement': True,
                      'externalCalls': 0}))
