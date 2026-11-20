"""1.0.21 default-teacher fixture: fake ``claude``/``codex`` executables and cases.

The fakes run as real child processes found on the child ``PATH``. They emit
scripted events and append what they received (argv, env, cwd, stdin) to
``calls.jsonl``. A scenario is read per invocation from ``<host>-queue.json``
(consumed in order) or ``<host>-scenario.json``. No account file, real CLI or
model is ever touched: ``PATH`` holds only the fake folder and system folders.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from unittest import mock

from memvane.learning.context_learning import edit_git_migration, general_teacher, shared
from memvane.learning.context_learning.contracts import digest, request
from memvane.learning.context_learning.store import canonical
from tests.support.case import FixtureCase

FAKE = r'''#!__PYTHON__
import json, os, sys, time
from pathlib import Path
HOST = __HOST__
LOGS = Path(__LOGS__)


def scenario():
    queue = LOGS / (HOST + '-queue.json')
    if queue.exists():
        items = json.loads(queue.read_text())
        if items:
            item = items.pop(0)
            queue.write_text(json.dumps(items))
            return item
    path = LOGS / (HOST + '-scenario.json')
    return json.loads(path.read_text()) if path.exists() else {}


def log(record):
    with open(LOGS / 'calls.jsonl', 'a') as out:
        out.write(json.dumps(record) + '\n')


def emit(value):
    sys.stdout.write(json.dumps(value) + '\n')
    sys.stdout.flush()


def answer(sc, ids):
    value = sc.get('answer', 'first')
    if value == 'first':
        return ids[:1]
    if value == 'empty':
        return []
    return value


record = {'host': HOST, 'stage': 'launch', 'argv': sys.argv[1:], 'env': dict(os.environ),
          'cwd': os.getcwd(), 'entries': sorted(os.listdir('.'))}

if HOST == 'codex' and sys.argv[1:2] == ['app-server']:
    # The quota read never consumes the exec queue.
    path = LOGS / 'codex-scenario.json'
    sc = json.loads(path.read_text()) if path.exists() else {}
    record['stage'] = 'app-server'
    log(record)
    first = json.loads(sys.stdin.readline())
    emit({'id': 1, 'result': {}})
    sys.stdin.readline()
    sys.stdin.readline()
    emit({'id': 2, 'result': {'account': {'type': 'chatgpt'}}})
    sys.stdin.readline()
    emit({'id': 3, 'result': {'rateLimitsByLimitId': {'codex': {'secondary': {
        'windowDurationMins': 10080, 'usedPercent': sc.get('usedPercent', 20)}}}}})
    sys.exit(0)

if HOST == 'codex':
    sc = scenario()
    raw = sys.stdin.read()
    record['stdin'] = raw
    log(record)
    payload = json.loads(raw)
    ids = [c['id'] for c in payload['candidates']]
    mode = sc.get('mode', 'ok')
    if mode == 'sleep':
        time.sleep(sc.get('seconds', 5))
    emit({'type': 'thread.started', 'thread_id': 'fixture'})
    emit({'type': 'turn.started'})
    for index, (kind, tool) in enumerate(sc.get('tools', [])):
        item = {'id': 'tool-%d' % index, 'type': kind, 'command': 'cat /secret/tool-argument'}
        if tool:
            item['tool'] = tool
        emit({'type': 'item.started', 'item': item})
        emit({'type': 'item.completed', 'item': dict(item, aggregated_output='tool-output-text')})
    messages = {'quota': "You've hit your usage limit. Try again later.",
                'auth': '401 Unauthorized: please log in again',
                'model': 'The requested model is not found',
                'content': 'This request was flagged by our content policy',
                'transport': 'stream disconnected: connection reset by peer'}
    if mode in messages:
        emit({'type': 'error', 'message': messages[mode]})
        sys.exit(1)
    if mode == 'exit':
        sys.exit(3)
    text = 'not json at all' if mode == 'invalid' else json.dumps({'selected_ids': answer(sc, ids)})
    emit({'type': 'item.completed', 'item': {'id': 'msg', 'type': 'agent_message', 'text': text}})
    emit({'type': 'turn.completed', 'usage': {'input_tokens': 900, 'cached_input_tokens': 100,
                                              'output_tokens': 12, 'reasoning_output_tokens': 4}})
    sys.exit(0)

# claude
sc = scenario()
init = sys.stdin.readline()
record['init'] = init
log(record)
request = json.loads(init)
if sc.get('initBeforeBody'):
    # Startup init before the initialize answer: the body must never follow.
    emit({'type': 'system', 'subtype': 'init', 'model': sc.get('initModel', sc.get('resolvedModel', 'claude-haiku-5-5')),
          'tools': sc.get('tools', []), 'mcp_servers': sc.get('mcp_servers', []),
          'plugins': sc.get('plugins', []), 'skills': sc.get('skills', [])})
resolved = sc.get('resolvedModel', 'claude-haiku-5-5')
models = [] if resolved is None else [{'value': 'haiku', 'resolvedModel': resolved}]
emit({'type': 'control_response', 'response': {'subtype': 'success',
      'request_id': request.get('request_id'),
      'response': {'commands': sc.get('commands', []), 'models': models}}})


def init_event():
    emit({'type': 'system', 'subtype': 'init', 'model': sc.get('initModel', resolved),
          'tools': sc.get('tools', []), 'mcp_servers': sc.get('mcp_servers', []),
          'plugins': sc.get('plugins', []), 'skills': sc.get('skills', [])})


line = sys.stdin.readline()
if not line:
    sys.exit(0)
log({'host': HOST, 'stage': 'body', 'body': line})
message = json.loads(line)
payload = json.loads(message['message']['content'])
ids = [c['id'] for c in payload['candidates']]
mode = sc.get('mode', 'ok')
if not sc.get('initBeforeBody'):
    init_event()
if mode == 'sleep':
    time.sleep(sc.get('seconds', 5))
if mode == 'hook':
    emit({'type': 'system', 'subtype': 'hook_started', 'hook_name': 'x'})
if mode == 'tool_event':
    emit({'type': 'tool_use', 'name': 'Read'})
errors = {'quota': ('rate_limit', "You've hit your limit"),
          'auth': ('authentication_failed', 'Invalid API key. Please run /login'),
          'content': (None, 'Request blocked by content policy')}
if mode in errors:
    kind, text = errors[mode]
    if kind:
        emit({'type': 'assistant', 'error': kind,
              'message': {'content': [{'type': 'text', 'text': text}]}})
    emit({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': text,
          'num_turns': 1})
    sys.stdin.read()
    sys.exit(1)
if mode == 'exit':
    sys.exit(3)
text = 'nope' if mode == 'invalid' else json.dumps({'selected_ids': answer(sc, ids)})
emit({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}})
emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': text, 'num_turns': 1,
      'usage': {'input_tokens': 700, 'cache_read_input_tokens': 50,
                'cache_creation_input_tokens': 0, 'output_tokens': 10}})
sys.stdin.read()
sys.exit(0)
'''

BODY = 'The cobalt garden card keeps marker zqcobaltx and its planting notes.'


def install(bin_dir, host, logs, *, python=None):
    path = Path(bin_dir) / host
    text = (FAKE.replace('__PYTHON__', python or sys.executable)
            .replace('__HOST__', repr(host)).replace('__LOGS__', repr(str(logs))))
    path.write_text(text)
    path.chmod(0o700)
    return path


def sampling(ids, selected):
    return {'mode': 'eligible-policy-pool', 'scope': 'queried-phases-only',
            'phase_counts': {'primary': len(ids)}, 'per_phase_limit': 30, 'eligible_ids': ids,
            'captured_ids': ids, 'baseline_selected_ids': selected, 'excluded': [],
            'snapshot_failures': [], 'retrieval_truncated': False, 'assessment_complete': True,
            'complete_within_returned_bound': True, 'index_revision': 'unavailable',
            'candidate_limit': 15}


def edit_sample(request_id, *, host='claude', text=None, count=3, selected=1, tag=''):
    rows = [{'candidate_id': f'wiki/card{index}.md',
             'revision_sha256': digest(BODY + tag + str(index)),
             'excerpt': (BODY + tag + str(index))[:600], 'eligible': True}
            for index in range(count)]
    ids = [row['candidate_id'] for row in rows]
    payload = {'prompt': text or f'Field notes {request_id}: the author keeps writing about the garden.',
               'context_origin': 'tool_edit',
               'edit_event': {'path_sha256': digest('notes/private-path.md'), 'after_sha256': None,
                              'operation': 'write', 'session_id': 'session-zq77',
                              'tool_use_id': 'tooluse-zq88', 'truncated': False,
                              'injected_ids': []}}
    sample = request(payload, rows, host=host, request_id=request_id,
                     sampling=sampling(ids, ids[:selected]))
    bodies = {row['candidate_id']: BODY + tag + str(index) for index, row in enumerate(rows)}
    return sample, bodies


def prompt_sample(request_id, *, host='codex'):
    rows = [{'candidate_id': 'wiki/card0.md', 'revision_sha256': digest(BODY),
             'excerpt': BODY, 'eligible': True}]
    sample = request({'prompt': 'Which card keeps the cobalt marker?'}, rows, host=host,
                     request_id=request_id, sampling=sampling(['wiki/card0.md'], []))
    return sample, {'wiki/card0.md': BODY}


class TeacherFixture(FixtureCase):
    """A namespace, a project, a fake user home and fake CLIs on the child PATH."""

    def setUp(self):
        super().setUp()
        self.root = self.home / 'ns'
        self.root.mkdir(mode=0o700)
        self.project = (self.home / 'project')
        self.project.mkdir()
        self.project = self.project.resolve()
        self.user = self.home / 'user'
        self.user.mkdir(mode=0o700)
        (self.user / '.codex').mkdir(mode=0o700)
        self.bin = self.home / 'bin'
        self.bin.mkdir(mode=0o700)
        self.logs = self.home / 'logs'
        self.logs.mkdir(mode=0o700)
        for host in ('claude', 'codex'):
            install(self.bin, host, self.logs)
        self.catalog()
        self.environ = {'HOME': str(self.user), 'PATH': f'{self.bin}:/usr/bin:/bin',
                        'TMPDIR': str(self.tmp), 'LANG': 'C', 'USER': 'fixture',
                        'LOGNAME': 'fixture'}
        self.policy = {'goldModel': 'gpt-6-luna', 'maxGoldCalls': 20, 'maxRssMib': 4096,
                       'safetySeconds': 60}
        self.proposal = {
            'schema': 'memvane-teacher-consent-v2', 'projectRoot': str(self.project),
            'purpose': 'selection-gold',
            'recipients': [{'host': 'claude', 'transport': 'claude-cli'},
                           {'host': 'codex', 'transport': 'codex-cli'}],
            'modelPolicy': 'lowest-tier', 'passes': 1,
            'fields': ['edit_text', 'candidate_body', 'candidate_id', 'revision_sha256'],
            'maxInputBytes': 65536, 'maxOutputBytes': 65536, 'timeoutSeconds': 60,
            'maxRssMib': 4096, 'grantedBy': 'default-enrollment-upgrade',
            'grantedAtUnixMs': 0, 'enabled': True}
        self.grant = {'projectRoot': str(self.project),
                      'operations': ['gold_default', 'local_train', 'quality_evaluate'],
                      'cohort': {'id': 'default-v1', 'notBeforeUnixMs': 0}}
        self.events = []
        marker = self.root / edit_git_migration._name(edit_git_migration.MARKER_PREFIX, self.project)
        marker.write_text(json.dumps({'schema': edit_git_migration.SCHEMA, 'status': 'not_affected',
                                      'count': 0, 'privateSamples': 0, 'migratedAtUnixMs': 1}))
        marker.chmod(0o600)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patches = stack
        stack.enter_context(mock.patch.object(general_teacher, '_policy', side_effect=lambda root: self.policy))
        stack.enter_context(mock.patch.object(general_teacher, '_scope_default',
                                              side_effect=lambda *a, **k: self.grant))
        stack.enter_context(mock.patch.object(general_teacher, '_consent_file_v2',
                                              side_effect=lambda root, project: dict(self.proposal)))
        stack.enter_context(mock.patch.object(general_teacher, '_case_active', return_value=True))
        stack.enter_context(mock.patch.object(general_teacher, 'resource_report',
                                              return_value={'ready': True}))
        stack.enter_context(mock.patch.object(general_teacher, '_emit',
                                              side_effect=lambda project, event, **f: self.events.append(dict(f, event=event))))

    def catalog(self, models=None, *, name='models_cache.json'):
        if models is None:
            models = [{'slug': 'gpt-6-luna', 'visibility': 'list',
                       'supported_reasoning_levels': [{'effort': 'low'}, {'effort': 'high'}]},
                      {'slug': 'gpt-6-sol', 'visibility': 'list',
                       'supported_reasoning_levels': [{'effort': 'low'}]}]
        path = self.user / '.codex' / name
        path.write_text(json.dumps({'fetched_at': '2026-11-10T00:00:00Z', 'models': models}))
        path.chmod(0o600)
        return path

    def scenario(self, host, value):
        (self.logs / f'{host}-scenario.json').write_text(json.dumps(value))

    def queue(self, host, values):
        (self.logs / f'{host}-queue.json').write_text(json.dumps(values))

    def records(self, host=None, stage='launch'):
        path = self.logs / 'calls.jsonl'
        if not path.exists():
            return []
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [row for row in rows if (host is None or row['host'] == host)
                and (stage is None or row['stage'] == stage)]

    def runs(self, host=None):
        return len(self.records(host))

    def add(self, request_id, *, host='claude', captured_at=None, sample=None, bodies=None,
            status='queued', **options):
        if sample is None:
            sample, bodies = edit_sample(request_id, host=host, **options)
        if captured_at is None:
            captured_at = 1000 + len(self.cases())
        with shared._db(self.root) as db:
            db.execute('INSERT INTO cases (project_key,project_root,request_id,input_sha256,sample,'
                       'bodies,corpus_fingerprint,captured_at,split,family_keys,use_state,status) '
                       'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                       (shared._project_key(self.project), str(self.project), request_id,
                        digest(canonical(sample)), canonical(sample), canonical(bodies), 'corpus',
                        captured_at, 'train', '[]', 'active', status))
        return sample, bodies

    def cases(self):
        with shared._db(self.root) as db:
            return {row[0]: row[1:] for row in db.execute(
                'SELECT request_id,status,error,use_state,first,second,gold FROM cases')}

    def stage(self, request_id):
        with shared._db(self.root) as db:
            row = db.execute("SELECT stage FROM project_gold_attempts WHERE request_id=?",
                             (request_id,)).fetchone()
        return row[0] if row else None

    def run_pass(self, **options):
        return general_teacher.process_pending_default(self.root, self.project, self.policy,
                                                       environ=self.environ, **options)

    def host_state(self):
        from memvane.learning.context_learning import teacher_state
        return teacher_state.read_state(self.root)

    def expire_waits(self):
        from memvane.learning.context_learning import teacher_state
        state = teacher_state.read_state(self.root)
        for row in state['hosts'].values():
            if row['waitUntilUnixMs'] is not None:
                row['waitUntilUnixMs'] = 1
        self.assertTrue(teacher_state.write_state(self.root, state))

    def teacher_events(self, outcome=None):
        return [e for e in self.events if e['event'] == 'learning.teacher'
                and (outcome is None or e.get('outcome') == outcome)]
