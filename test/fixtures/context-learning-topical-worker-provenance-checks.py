"""Disposable multisource worker lineage, QMD ACK, retirement and reuse."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, 'core')
import qmd_route
import topical_story_scope as scope
import wiki_topical as topical
import wiki_topical_publish as publisher
from context_learning import source_card_provenance as host, topical_worker_provenance as worker
from context_learning import synthetic_experiment as experiment
from context_learning.shared import _db


def sha(raw): return hashlib.sha256(raw).hexdigest()


with tempfile.TemporaryDirectory(prefix='qmd-worker-proof-') as temp:
    base = Path(temp).resolve()
    project = base/'novel'; project.mkdir(mode=0o700)
    story = project/'planned-story'; story.mkdir(mode=0o700)
    shared = project/'shared'; shared.mkdir(mode=0o700)
    wiki = story/'.auto-context/wiki'; wiki.mkdir(parents=True)
    for name, text in [('01_Settings/first.md','The amber bell marks dawn.\n'),
                       ('02_Characters/second.md','The silver bell marks dusk.\n')]:
        file = story/name; file.parent.mkdir(parents=True,exist_ok=True)
        file.write_text(text)
    source_names = ['01_Settings/first.md','02_Characters/second.md']
    routes = {'selected':True,'INDEX_PATH':str(base/'index.sqlite')}
    with sqlite3.connect(routes['INDEX_PATH']) as index:
        index.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        index.execute('CREATE TABLE content_vectors(hash TEXT PRIMARY KEY)')
    gids = ['a'*24,'b'*24]
    sidecars, cards, records = {}, {}, {}
    scope_value = {'story':story,'project':project,'wiki':wiki,
                   'collection':scope.WIKI_COLLECTION,'policySha256':'f'*64}
    def exact(value): return Path(value).resolve() == story
    def snapshot(name):
        with patch.object(scope,'exact_story',side_effect=exact), \
             patch.object(scope,'read',return_value=scope_value):
            return topical.source_snapshot(story,name,{})[0]
    def make_card(gid, names):
        cid = 'two-bells'
        page = wiki/'topical-v2'/gid/(cid+'.md')
        page.parent.mkdir(parents=True,exist_ok=True)
        body = ('---\ntitle: Two bells\nstatus: verified\n---\n' + gid + '\n').encode()
        page.write_bytes(body)
        sidecars[gid] = json.dumps({'sourceRevisions':[snapshot(n) for n in names]},
                                   sort_keys=True).encode()
        cards[gid] = body
        records[gid] = {'status':'backend_pass','mode':'backend','verdict':'pass',
                        'generationId':gid,'cardId':cid}
        proof = story/'topical-publication-proofs'/gid/(cid+'.json')
        proof.parent.mkdir(parents=True,exist_ok=True)
        proof.write_text(json.dumps({'generationId':gid,'approved':True}))
        return page
    first = make_card(gids[0],source_names)
    def published(_root,gid,cid):
        raw = cards[gid]
        return {'sha256':sha(raw),'path':str(wiki/'topical-v2'/gid/(cid+'.md'))}
    def generation_bytes(_root,gid,relative):
        return sidecars[gid] if relative.endswith('.evidence.json') else cards[gid]
    def index_sources(names):
        with sqlite3.connect(routes['INDEX_PATH']) as index:
            for name in names:
                rev = snapshot(name)
                index.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,1)',
                    (rev['collection'],name.split('/',1)[1],rev['sha256']))
    def index_card(page):
        raw = page.read_bytes()
        with sqlite3.connect(routes['INDEX_PATH']) as index:
            index.execute('INSERT OR REPLACE INTO documents VALUES (?,?,?,1)',
                (scope.WIKI_COLLECTION,page.relative_to(wiki).as_posix(),sha(raw)))
            index.execute('INSERT OR IGNORE INTO content_vectors VALUES (?)',(sha(raw),))
    patches = [
        patch.object(worker,'NOVEL_ROOT',project),
        patch.object(worker,'story_for',return_value=story),
        patch.object(worker,'shared_for',return_value=shared),
        patch.object(host,'NOVEL_ROOT',project),
        patch.object(host,'story_for',return_value=story),
        patch.object(host,'shared_for',return_value=shared),
        patch.object(experiment,'story_for',return_value=story),
        patch.object(experiment,'shared_for',return_value=shared),
        patch.object(scope,'exact_story',side_effect=exact),
        patch.object(scope,'read',return_value=scope_value),
        patch.object(qmd_route,'project_paths',return_value=routes),
        patch.object(publisher,'publish',side_effect=published),
        patch.object(publisher,'_attested',side_effect=lambda _r,gid,_cid:records[gid]),
        patch.object(publisher,'_publication_proof',return_value={'valid':True}),
        patch.object(topical,'read_generation_bytes',side_effect=generation_bytes),
        patch.object(worker,'_provider_receipts',return_value='e'*64),
    ]
    import contextlib
    with contextlib.ExitStack() as stack:
        for item in patches: stack.enter_context(item)
        index_card(first)
        try: worker.register(shared,project,gids[0],'two-bells')
        except ValueError as exc: assert str(exc) == 'topical_worker_index_unacknowledged'
        else: raise AssertionError('missing_source_ack_accepted')
        index_sources(source_names)
        first_result = worker.register(shared,project,gids[0],'two-bells')
        assert first_result['status'] == 'registered'
        assert worker.register(shared,project,gids[0],'two-bells')['status'] == 'already_registered'
        main_sha = sha((story/'topical-publication-proofs'/gids[0]/
                       'two-bells.json').read_bytes())
        with patch.object(publisher,'_publication_proof',
                          return_value={'createReviewSha256':'d'*64}):
            extra = worker._evidence(story,project,scope_value,gids[0],
                                     'two-bells',require_index=True)
            assert extra['publicationSha256'] == topical.digest(topical.encoded([
                'qmd-publication-review-pair-v1',main_sha,'d'*64]))
        candidate = scope.WIKI_COLLECTION+'/topical-v2/'+gids[0]+'/two-bells.md'
        with _db(shared) as db:
            assert host.registered_revision(db,shared,project,candidate,sha(first.read_bytes()))
            assert len(db.execute('SELECT 1 FROM topical_worker_versions').fetchall()) == 1
            original_ack=db.execute('SELECT index_ack FROM topical_worker_versions').fetchone()[0]
            db.execute('UPDATE topical_worker_versions SET index_ack=?',('[]',))
            assert not worker.registered_revision(db,shared,project,candidate,
                                                   sha(first.read_bytes()),require_index=False)
            db.execute('UPDATE topical_worker_versions SET index_ack=?',(original_ack,))
        proof = story/'topical-publication-proofs'/gids[0]/'two-bells.json'
        proof_bytes = proof.read_bytes(); proof.unlink()
        with _db(shared) as db:
            assert not host.registered_revision(db,shared,project,candidate,sha(first.read_bytes()))
        assert not proof.exists()
        proof.write_bytes(proof_bytes)
        with sqlite3.connect(routes['INDEX_PATH']) as index:
            index.execute('UPDATE documents SET hash=? WHERE collection=? AND path=?',
                          ('0'*64,scope.WIKI_COLLECTION,first.relative_to(wiki).as_posix()))
        with _db(shared) as db:
            assert not host.registered_revision(db,shared,project,candidate,sha(first.read_bytes()))
        index_card(first)
        second = make_card(gids[1],[source_names[1]])
        index_card(second)
        archive = story/'topical-retired'/gids[0]/'two-bells.md'
        archive.parent.mkdir(parents=True)
        first.rename(archive)
        with sqlite3.connect(routes['INDEX_PATH']) as index:
            index.execute('UPDATE documents SET active=0 WHERE collection=? AND path=?',
                          (scope.WIKI_COLLECTION,'topical-v2/'+gids[0]+'/two-bells.md'))
        (story/source_names[0]).unlink()
        with sqlite3.connect(routes['INDEX_PATH']) as index:
            index.execute('UPDATE documents SET active=0 WHERE collection=?',
                          (scope.SOURCE_COLLECTIONS['01_Settings'],))
        result = worker.reconcile_pending(shared,project)
        assert result['retired'] == 1 and len(result['registered']) == 1 and not result['deferred']
        candidate2 = scope.WIKI_COLLECTION+'/topical-v2/'+gids[1]+'/two-bells.md'
        with _db(shared) as db:
            assert not host.registered_revision(db,shared,project,candidate,sha(archive.read_bytes()))
            assert host.registered_revision(db,shared,project,candidate2,sha(second.read_bytes()))
            latest = host.latest_paths(db)
            assert source_names[0] not in latest and latest[source_names[1]][1] == snapshot(source_names[1])['sha256']
print(json.dumps({'sourceAckRequired':True,'oneRegistration':True,'indexTamperDenied':True,
                  'missingProofDenied':True,
                  'sharedReferenceRetained':True,'retirementExcluded':True}))
