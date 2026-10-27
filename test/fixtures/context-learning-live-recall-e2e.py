"""Real recall pipeline with a synthetic, injected local selector transport."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

with tempfile.TemporaryDirectory(prefix='qmd-live-recall-', dir=Path.home() / 'work') as name:
    base = Path(name).resolve()
    project = base / 'project'
    wiki = project / '.auto-context/wiki'
    sources = project / 'docs'
    wiki.mkdir(parents=True)
    sources.mkdir()
    state_parent = base / 'private-state'
    state_parent.mkdir(mode=0o700)
    namespace = hashlib.sha256(str(project).encode()).hexdigest()
    state = state_parent / namespace
    state.mkdir(mode=0o700)
    artifact = state / 'synthetic-base.artifact'
    artifact.write_text('synthetic-only artifact')
    artifact.chmod(0o600)
    active = state / 'active-checkpoint.json'
    active.write_text(json.dumps({'schema_version': 1, 'artifact_path': str(artifact),
        'artifact_sha256': hashlib.sha256(artifact.read_bytes()).hexdigest()}))
    active.chmod(0o600)
    index = base / 'qmd-db/index.sqlite'
    index.parent.mkdir()
    qmd_config = base / 'qmd-config/index.yml'
    qmd_config.parent.mkdir()
    qmd_config.write_text('collections: {wiki: {path: .auto-context/wiki}}\nmodels: {embed: synthetic}\n')
    hits = []
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents (collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        for i in range(16):
            source = sources / f'source{i:02}.md'
            source.write_text(f'Synthetic source record {i:02}.\n')
            info = source.stat()
            revision = {'kind': 'file', 'path': 'docs/' + source.name,
                        'collection': 'docs', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'size': info.st_size, 'mtimeNs': info.st_mtime_ns}
            card = wiki / f'card{i:02}.md'
            card.write_text('---\ntitle: Synthetic card ' + str(i) + '\nstatus: verified\n'
                'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                + json.dumps(revision) + '\n---\n'
                + f'Synthetic complete wiki body for card {i:02}; source details are local.\n')
            db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                       ('wiki', card.name, hashlib.sha256(card.read_bytes()).hexdigest()))
            hits.append({'file': 'wiki/' + card.name, 'score': 1 - i / 100,
                         'title': 'Synthetic', 'line': 1})
    (project / '.auto-context/settings.json').write_text(json.dumps({
        'indexing': True, 'collections': ['wiki'],
        'collectionPaths': {'wiki': '.auto-context/wiki'},
        'collectionRoles': {'wiki': 'wiki'}, 'recallStrategy': 'wikiOnly',
        'topN': 3, 'minScore': 0, 'events': ['userPromptSubmit'],
        'contextLearning': {'capture': True, 'stateRoot': str(state_parent)}}))
    fixture = base / 'hits.json'
    fixture.write_text(json.dumps({'results': hits}))
    log = base / 'recall.log'
    code = '''import json,os,sys,time
sys.path.insert(0,'core')
import context_learning.live_select as selector
import context_learning.adaptive_pool as planner
if os.environ['QMD_LIVE_CASE'] in ('shared_30','shared_5'):
    planner.plan=lambda prompt,deadline,**_kwargs:{'requested':5 if os.environ['QMD_LIVE_CASE']=='shared_5' else 30,'reason':'admitted'}
def choose(state,prompt,rows,*,corpus_fingerprint,deadline=None,**options):
    assert len(rows)==15 and all('Synthetic complete wiki body' in r['source_text'] for r in rows)
    assert corpus_fingerprint and len(corpus_fingerprint)==64
    assert deadline is not None
    case=os.environ['QMD_LIVE_CASE']
    if case=='profile':
        assert str(state)==os.environ['QMD_PROFILE_ROOT'] and not options
    if case in ('selected','profile'):return ['wiki/card14.md','wiki/card02.md'],'selected',{'elapsed_seconds':.01,'model_version':'fixture-cycle','model_input_view':'full-candidate-1024-v1'}
    if case=='zero':return [],'selected',{'elapsed_seconds':.01}
    if case=='deadline':time.sleep(5)
    return None,'timeout',{}
if os.environ['QMD_LIVE_CASE'] != 'real_missing_runtime':
    selector.choose=choose
if os.environ['QMD_LIVE_CASE']=='profile':
    from pathlib import Path
    from context_learning import learning_profile as profile,shared
    root=Path(os.environ['QMD_PROFILE_ROOT']);project=Path(os.environ['QMD_PROFILE_PROJECT'])
    settings=json.loads((project/'.auto-context/settings.json').read_text())
    row={'schema':profile.SCHEMA,'namespaceRoot':str(root),'projectRoot':str(project),'choice':'local-auto-apply',
         'expiresAtUnixMs':int(time.time()*1000)+60000,'settings':settings,'settingsBindingSha256':profile.digest(profile.binding(settings))}
    file=root/profile.NAME;file.write_text(json.dumps({'proposal':row,'acceptedSha256':profile.digest(row)}));file.chmod(0o600)
    shared._policy=lambda _:{} # Scope/model admission is tested separately; this case proves recall routing.
    def forbidden(*args,**kwargs):raise AssertionError('legacy capture/teacher consent path used')
    shared._current_config=forbidden;shared.trial_eligible=forbidden
    planner.plan=lambda *args,**kwargs:{'requested':15,'reason':'admitted'}
import recall
import hook_main
hook_main.run(recall.main)
'''
    env = {**os.environ, 'QMD_QUERY_FIXTURE': str(fixture),
           'QMD_SETUP_GUARD_FIXTURE': '1',
           'QMD_RECALL_LOG': str(log), 'INDEX_PATH': str(index),
           'QMD_CONFIG_DIR': str(qmd_config.parent), 'PYTHONDONTWRITEBYTECODE': '1'}
    def run(case):
        result = subprocess.run([sys.executable, '-c', code],
            input=json.dumps({'prompt': 'synthetic source record relevance', 'cwd': str(project),
                              'hook_event_name': 'UserPromptSubmit'}),
            env={**env, 'QMD_LIVE_CASE': case}, text=True,
            capture_output=True, timeout=20, check=True)
        lines = [json.loads(line) for line in log.read_text().splitlines() if line.startswith('{')]
        events = [row for row in lines if row['event'] == 'qmd_laya_selection']
        return result.stdout, events[-1]
    selected, event = run('selected')
    assert selected.index('card14.md') < selected.index('card02.md')
    assert 'card00.md' not in selected
    assert event['candidates'] == 15 and event['selected'] == 2 and not event['fallback']
    assert event['model_version'] == 'fixture-cycle'
    assert event['model_input_view'] == 'full-candidate-1024-v1'
    zero, event = run('zero')
    assert not zero.strip() and event['reason'] == 'selected' and event['selected'] == 0
    fallback, event = run('fallback')
    assert all(f'card{i:02}.md' in fallback for i in range(3))
    assert event['reason'] == 'timeout' and event['fallback']
    started = __import__('time').monotonic()
    expired = subprocess.run([sys.executable, '-c', code],
        input=json.dumps({'prompt': 'synthetic source record relevance', 'cwd': str(project),
                          'hook_event_name': 'UserPromptSubmit'}),
        env={**env, 'QMD_LIVE_CASE': 'deadline', 'QMD_HOOK_TOTAL_SECONDS': '2'},
        text=True, capture_output=True, timeout=4, check=True)
    assert not expired.stdout.strip()
    assert __import__('time').monotonic() - started < 3.5
    assert 'qmd_hook_deadline_exceeded' in log.read_text()
    policy = state / 'live-selector.json'
    missing_model = base / 'missing-model'; missing_model.mkdir()
    policy.write_text(json.dumps({'schema': 'qmd-live-selector-v1',
        'trainerArgv': [sys.executable, str(Path('core/context_learning/laya_adapter.py').resolve()),
                        str(missing_model)], 'timeoutSeconds': 2, 'maxRssMib': 512}))
    policy.chmod(0o600)
    unsupported, event = run('real_missing_runtime')
    assert all(f'card{i:02}.md' in unsupported for i in range(3))
    assert event['fallback'] and event['reason'] in ('runtime_unavailable', 'timeout')
    saved_db = state_parent / namespace / 'learning.sqlite3'
    with sqlite3.connect(saved_db) as db:
        samples = [json.loads(row[0]) for row in db.execute('SELECT body FROM samples')]
    assert len(samples) == 4 and all(s['sampling']['candidate_limit'] == 15
                                     and len(s['candidates']) == 15
                                     and len(s['sampling']['index_revision']) == 64
                                     for s in samples)
    stale_card = wiki / 'card00.md'
    original_card = stale_card.read_text()
    stale_card.write_text(original_card + '\nUnindexed new text.\n')
    stale_output, event = run('selected')
    assert event['fallback'] and event['reason'] == 'candidate_unavailable'
    assert all(f'card{i:02}.md' in stale_output for i in range(3))
    stale_card.write_text(original_card)
    settings_path = project / '.auto-context/settings.json'
    settings = json.loads(settings_path.read_text())
    previous_settings=json.loads(settings_path.read_text())
    profile_root=base/'accepted-profile';profile_root.mkdir(mode=0o700)
    (profile_root/'active-checkpoint.json').write_bytes(active.read_bytes());(profile_root/'active-checkpoint.json').chmod(0o600)
    settings['contextLearning']['capture']=False
    settings['sharedLearning']={'enabled':True,'profile':'qmd-local-learning-profile-v1','autoApply':True,'root':str(profile_root)}
    settings_path.write_text(json.dumps(settings))
    env.update({'QMD_PROFILE_ROOT':str(profile_root),'QMD_PROFILE_PROJECT':str(project)})
    routed,event=run('profile')
    assert not event['fallback'] and event['selected']==2 and 'card14.md' in routed,event
    settings=previous_settings
    settings['topN'] = 5
    settings['contextLearning']['liveSelection'] = False
    settings['sharedLearning'] = {'enabled': True, 'trialSelection': True,
                                  'root': str(base / 'missing-shared-grant')}
    settings_path.write_text(json.dumps(settings))
    no_grant, event = run('shared_30')
    assert all(f'card{i:02}.md' in no_grant for i in range(5)), no_grant
    assert 'card05.md' not in no_grant
    assert event['fallback'] and event['reason'] == 'shared_state_unavailable'
    assert event['requested'] == 30 and event['available'] == 16 and event['scored'] == 0
    with sqlite3.connect(saved_db) as db:
        latest = json.loads(db.execute('SELECT body FROM samples ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    assert latest['sampling']['candidate_limit'] == 30 and len(latest['candidates']) == 16
    limited, event = run('shared_5')
    assert event['requested'] == 5 and event['available'] == 5
    with sqlite3.connect(saved_db) as db:
        limited_sample = json.loads(db.execute('SELECT body FROM samples ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    assert (limited_sample['sampling']['candidate_limit'] == 5
            and len(limited_sample['candidates']) == 5
            and limited_sample['sampling']['phase_counts']['primary'] == 16
            and not limited_sample['sampling']['assessment_complete']
            and not limited_sample['sampling']['complete_within_returned_bound'])
    with sqlite3.connect(index) as db:
        for i in range(16, 32):
            source = sources / f'source{i:02}.md'
            source.write_text(f'Synthetic source record {i:02}.\n')
            info = source.stat()
            revision = {'kind': 'file', 'path': 'docs/' + source.name,
                        'collection': 'docs', 'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                        'size': info.st_size, 'mtimeNs': info.st_mtime_ns}
            card = wiki / f'card{i:02}.md'
            card.write_text('---\ntitle: Synthetic card ' + str(i) + '\nstatus: verified\n'
                'createdBy: qmd-auto-context\nsourceRevisions:\n  - '
                + json.dumps(revision) + '\n---\n'
                + f'Synthetic complete wiki body for card {i:02}; source details are local.\n')
            db.execute('INSERT INTO documents VALUES (?,?,?,1)',
                       ('wiki', card.name, hashlib.sha256(card.read_bytes()).hexdigest()))
            hits.append({'file': 'wiki/' + card.name, 'score': 1 - i / 100,
                         'title': 'Synthetic', 'line': 1})
    fixture.write_text(json.dumps({'results': hits}))
    _over, event = run('shared_30')
    assert event['requested'] == 30 and event['available'] == 30 and not event['completeness']
    with sqlite3.connect(saved_db) as db:
        over_sample = json.loads(db.execute('SELECT body FROM samples ORDER BY rowid DESC LIMIT 1').fetchone()[0])
    assert (over_sample['sampling']['phase_counts']['primary'] == 32
            and over_sample['sampling']['retrieval_truncated']
            and not over_sample['sampling']['complete_within_returned_bound'])
    print(json.dumps({'liveTop15': True, 'relevanceOrder': True, 'semanticZero': True,
                      'timeoutFallback': True, 'unsupportedRuntimeFallback': True,
                      'captureMatchesLivePool': True, 'staleIndexFallback': True,
                      'sharedWithoutGrantKeepsTopN': True,
                      'adaptiveWiderCapture': True,
                      'overReturnNeverComplete': True,
                      'acceptedProfileWithoutCaptureRoutesSharedActive':True,
                      'externalCalls': 0}))
