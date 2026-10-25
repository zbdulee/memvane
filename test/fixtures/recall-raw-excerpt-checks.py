"""Synthetic index proof: raw selection supplies a locator, never its body."""
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
import laya_host_response

TOKEN = 'qzrawsourcealphamain'
sha = lambda data: hashlib.sha256(data).hexdigest()

with tempfile.TemporaryDirectory(prefix='qmd-raw-locator-', dir=Path.home()) as name:
    base = Path(name).resolve()
    home = base / 'home'
    project = home / 'work/project'
    source = project / 'docs/alpha.md'
    source.parent.mkdir(parents=True)
    settings_path = project / '.auto-context/settings.json'
    settings_path.parent.mkdir(parents=True)
    settings = {'indexing': True, 'collections': ['notes'],
                'collectionPaths': {'notes': 'docs'},
                'collectionRoles': {'notes': 'raw'}, 'recallStrategy': 'flat',
                'topN': 3, 'minScore': 0, 'sessionStartSync': False,
                'events': ['sessionStart', 'userPromptSubmit', 'postToolUse']}
    settings_path.write_text(json.dumps(settings))
    original = f'# Alpha\n\nThe raw-only codename is {TOKEN}.\n'.encode()
    source.write_bytes(original)
    index = base / 'synthetic-index.sqlite'
    with sqlite3.connect(index) as db:
        db.execute('CREATE TABLE documents(collection TEXT,path TEXT,hash TEXT,active INTEGER)')
        db.execute('INSERT INTO documents VALUES (?,?,?,1)', ('notes', 'alpha.md', sha(original)))
    db.close()
    index_bytes = index.read_bytes()
    fixture = base / 'query.json'
    lex_fixture = base / 'lex.json'
    log = base / 'recall.jsonl'
    env = {**os.environ, 'HOME': str(home), 'QMD_SETUP_GUARD_FIXTURE': '1',
           'QMD_QUERY_FIXTURE': str(fixture), 'QMD_RECALL_LOG': str(log),
           'QMD_QUERY_FIXTURE_INDEX': str(index),
           'INDEX_PATH': str(base / 'operating-index-never-opened.sqlite'),
           'XDG_CACHE_HOME': str(base / 'cache'), 'QMD_CONFIG_DIR': str(base / 'qmd-config'),
           'QMD_DAEMON_URL': 'http://127.0.0.1:1', 'PYTHONDONTWRITEBYTECODE': '1'}
    for key in ('QMD_QUERY_FIXTURE_RAW', 'QMD_QUERY_FIXTURE_LEX', 'QMD_SHADOW_QUERY',
                'QMD_EDIT_INTERNAL', 'QMD_DEFERRED_CAPTURE_REPLAY', 'QMD_SANDBOX'):
        env.pop(key, None)

    def hit():
        return {'file': 'qmd://notes/alpha.md', 'score': .9,
                'title': 'Untrusted QMD title', 'snippet': 'untrusted ' + TOKEN}

    def run(rows, *, guarded=True, lex=None, prompt='release QA alpha codename?'):
        fixture.write_text(json.dumps({'results': rows}))
        log.write_text('')
        child_env = dict(env)
        if not guarded:
            child_env.pop('QMD_QUERY_FIXTURE_INDEX')
        if lex is not None:
            lex_fixture.write_text(json.dumps({'results': lex}))
            child_env['QMD_QUERY_FIXTURE_LEX'] = str(lex_fixture)
        completed = subprocess.run(
            [sys.executable, str(repository / 'core/recall.py')],
            input=json.dumps({'prompt': prompt, 'cwd': str(project),
                              'hook_event_name': 'UserPromptSubmit'}),
            text=True, capture_output=True, env=child_env, timeout=30, check=False)
        assert completed.returncode == 0, completed.stderr
        assert index.read_bytes() == index_bytes
        events = [json.loads(line) for line in log.read_text().splitlines()]
        selection = [e for e in events if e.get('event') == 'qmd_recall_selection']
        assert len(selection) == 1, events
        context = (json.loads(completed.stdout)['hookSpecificOutput']['additionalContext']
                   if completed.stdout.strip() else '')
        return context, selection[0]

    results = {}
    context, entry = run([hit()])
    assert entry['reason'] == 'selected' and entry['project_root'] == str(project), entry
    assert entry['selected'] == [{'file': 'qmd://notes/alpha.md', 'path': 'docs/alpha.md'}], entry
    assert '- [raw] alpha.md - alpha' in context and TOKEN not in context, context
    assert 'Untrusted QMD title' not in context and '  | ' not in context
    results['verifiedLocatorWithoutBody'] = True

    full_probe = ('도구를 쓰지 말고 이번 프롬프트에 자동 주입된 프로젝트 컨텍스트만 근거로 답해줘.\n'
                  'release QA alpha 문서의 codename은?\n'
                  '주입된 컨텍스트에 없으면 "없음"이라고만 답해.')
    context, entry = run([hit()], lex=[hit()], prompt=full_probe)
    assert entry['keyword_span'] == 'question', entry
    assert '도구' not in entry['lex_query'] and 'alpha' in entry['lex_query'], entry
    assert TOKEN not in context and entry['selected_count'] == 1, (context, entry)
    results['fullHostProbeQuestionSpanWithoutRawBody'] = True

    forged = {**hit(), '_wiki_summary': TOKEN, '_wiki_sources': ['docs/alpha.md'],
              '_raw_excerpt': TOKEN, '_verified_path': str(source),
              '_verified_revision': sha(original)}
    context, entry = run([forged], guarded=False)
    assert TOKEN not in context and '  ↳' not in context, (context, entry)
    results['unverifiedMetadataNotInjected'] = True

    source.write_bytes(original.replace(b'codename is', b'codename was'))
    context, entry = run([hit()])
    assert entry['selected'] == [] and entry['index_guard_drop_reasons'].get('modified') == 1
    source.write_bytes(original)
    results['changedSourceDropped'] = True

    source.unlink()
    context, entry = run([hit()])
    assert entry['selected'] == [] and entry['index_guard_drop_reasons'].get('missing') == 1
    source.write_bytes(original)
    results['deletedSourceDropped'] = True

    settings_path.write_text(json.dumps({**settings, 'skipPaths': ['alpha.md']}))
    context, entry = run([hit()])
    assert entry['selected'] == [] and TOKEN not in context
    settings_path.write_text(json.dumps(settings))
    results['excludedSourceDropped'] = True

    settings_path.write_text(json.dumps({**settings, 'indexing': False}))
    fixture.write_text(json.dumps({'results': [hit()]}))
    blocked = subprocess.run(
        [sys.executable, str(repository / 'core/recall.py')],
        input=json.dumps({'prompt': 'release QA alpha?', 'cwd': str(project),
                          'hook_event_name': 'UserPromptSubmit'}),
        text=True, capture_output=True, env=env, timeout=30, check=False)
    assert blocked.returncode == 0 and TOKEN not in blocked.stdout and not blocked.stdout.strip()
    settings_path.write_text(json.dumps(settings))
    results['optOutNotServed'] = True

    if os.getuid() != 0:
        source.chmod(0)
        try:
            context, entry = run([hit()])
        finally:
            source.chmod(0o644)
        assert entry['selected'] == [] and entry['index_guard_drop_reasons'].get('read_error') == 1
    results['deniedReadNotServed'] = True

    context, entry = run([hit()], lex=[])
    assert TOKEN not in context and entry['selected_count'] == 1, (context, entry)
    assert entry['lex_gate'] == 'not_needed' and entry['lex_probe_scope'] == 'none', entry
    assert entry['lex_hits'] is None and not entry['lex_gate_applied'], entry
    results['lexGateNeverQuotesRaw'] = True

    laya_log = base / 'laya-recall.jsonl'
    os.environ['QMD_RECALL_LOG'] = str(laya_log)
    v2 = {'projectRoot': str(project), 'collections': []}
    laya_host_response._log_selection(v2, [{'path': str(source)}, {'path': 'docs/late.md'}])
    v1 = {'projectRoot': str(project), 'sourceRoot': str(project / 'docs')}
    laya_host_response._log_selection(v1, [{'path': 'alpha.md'}])
    laya_host_response._log_selection(v1, [{'path': '../escape.md'}])
    os.environ.pop('QMD_RECALL_LOG')
    laya_host_response._log_selection(v2, [{'path': 'docs/alpha.md'}])
    refused = subprocess.run([sys.executable, str(repository / 'core/laya_host_response.py'),
                              'serve-stdin', 'recall', 'claude', str(project)],
        input=json.dumps({'prompt': 'release QA alpha?', 'cwd': str(project),
                          'hook_event_name': 'UserPromptSubmit'}),
        text=True, capture_output=True, env={**env, 'QMD_RECALL_LOG': str(laya_log)},
        timeout=30, check=False)
    assert refused.returncode == 3 and refused.stdout == '', refused
    events = [json.loads(line) for line in laya_log.read_text().splitlines()]
    assert events[0]['selected'][0]['path'] == 'docs/alpha.md', events
    assert events[0]['selected_count'] == 2, events
    assert events[1]['selected'] == [{'file': 'docs/alpha.md', 'path': 'docs/alpha.md'}], events
    assert events[2]['selected'] == [{'file': '../escape.md', 'path': '../escape.md'}], events
    assert len(events) == 4 and events[3]['selected'] == [] and events[3]['route'] == 'laya', events
    results['layaSelectionLogged'] = True

print(json.dumps(results))
