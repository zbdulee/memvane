"""Offline one-use dispatch receipts never turn reservations into calls."""
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from context_learning import gold_receipt
from context_learning.synthetic_teacher import _invoke_codex


def reject(action, reason):
    try:
        action()
    except ValueError as error:
        assert reason in str(error), str(error)
    else:
        raise AssertionError(f'expected {reason}')


db = sqlite3.connect(':memory:')
db.execute('PRAGMA foreign_keys=ON')
db.execute('CREATE TABLE experiment_calls (call_key TEXT PRIMARY KEY, kind TEXT)')
keys = [f'gold:synthetic-{i}:first_running' for i in range(5)]
db.executemany('INSERT INTO experiment_calls VALUES (?,?)', [(key, 'gold') for key in keys])
gold_receipt.ensure_schema(db)
assert db.execute('SELECT count(*) FROM gold_attempt_receipts').fetchone()[0] == 0
assert gold_receipt.runtime_version().startswith(gold_receipt.SCHEMA + ':')

gold_receipt.reserve(db, keys[0], 'a'*64, transport='codex_cli')
reject(lambda: gold_receipt.reserve(db, keys[0], 'a'*64, transport='codex_cli'),
       'gold_receipt_already_reserved')
gold_receipt.failure(db, keys[0], ValueError('synthetic_collection_unproven'))
assert db.execute('SELECT stage,error_code,child_pid FROM gold_attempt_receipts WHERE call_key=?',
                  (keys[0],)).fetchone() == ('pre_send_rejected',
                                             'synthetic_collection_unproven', None)

gold_receipt.reserve(db, keys[1], 'b'*64, transport='codex_cli')
gold_receipt.transition(db, keys[1], ('reserved',), 'dispatch_unknown')
gold_receipt.failure(db, keys[1], ValueError('crash_between_receipt_and_spawn'))
assert db.execute('SELECT stage,child_pid FROM gold_attempt_receipts WHERE call_key=?',
                  (keys[1],)).fetchone() == ('dispatch_unknown', None)

gold_receipt.reserve(db, keys[2], 'c'*64, transport='codex_cli')
gold_receipt.transition(db, keys[2], ('reserved',), 'dispatch_unknown')
gold_receipt.transition(db, keys[2], ('dispatch_unknown',), 'dispatched', child_pid=123)
gold_receipt.failure(db, keys[2], ValueError('provider_error'))
assert db.execute('SELECT stage,error_code,child_pid FROM gold_attempt_receipts WHERE call_key=?',
                  (keys[2],)).fetchone() == ('dispatched_uncertain', 'provider_error', 123)

gold_receipt.reserve(db, keys[3], 'd'*64, transport='codex_cli')
gold_receipt.transition(db, keys[3], ('reserved',), 'dispatch_unknown')
gold_receipt.transition(db, keys[3], ('dispatch_unknown',), 'dispatched', child_pid=124)
answer_sha = hashlib.sha256(b'{"selected_ids":[]}').hexdigest()
gold_receipt.transition(db, keys[3], ('dispatched',), 'answer_received',
                        answer_sha256=answer_sha)
gold_receipt.transition(db, keys[3], ('answer_received',), 'committed')
assert db.execute('SELECT stage,answer_sha256 FROM gold_attempt_receipts WHERE call_key=?',
                  (keys[3],)).fetchone() == ('committed', answer_sha)
reject(lambda: gold_receipt.transition(db, keys[3], ('answer_received',), 'committed'),
       'gold_receipt_transition_conflict')

gold_receipt.reserve(db, keys[4], 'e'*64, transport='injected')
gold_receipt.failure(db, keys[4], ValueError('synthetic fake raised'))
assert db.execute('SELECT stage,error_code FROM gold_attempt_receipts WHERE call_key=?',
                  (keys[4],)).fetchone() == ('dispatch_unknown', 'valueerror')
assert db.execute('SELECT count(*) FROM gold_attempt_receipts').fetchone()[0] == 5

# Exercise the actual local spawn boundary without a provider or network call.
class FakeProcess:
    def __init__(self, _args, *, stdout, **_kwargs):
        self.pid = 12345
        self.returncode = 0
        stdout.write((json.dumps({'type': 'item.completed', 'item': {
            'type': 'agent_message', 'text': '{"selected_ids":[]}'}}) + '\n').encode())
        stdout.write((json.dumps({'type': 'turn.completed', 'usage': {
            'input_tokens': 1, 'output_tokens': 1}}) + '\n').encode())
        stdout.flush()

    def poll(self):
        return 0


with tempfile.TemporaryDirectory(prefix='qmd-fake-gold-', dir='/tmp') as folder:
    sample = {'prompt': {'text': 'Invented question'}, 'candidates': []}
    policy = {'maxInputBytes': 10000, 'maxOutputBytes': 10000, 'model': 'fake'}
    worker = {'maxRssMib': 100, 'safetySeconds': 2}
    events = []
    with patch('context_learning.synthetic_teacher._scope_still_active'), patch(
            'context_learning.synthetic_teacher.codex_adapter.argv', return_value=['codex']), patch(
            'context_learning.synthetic_teacher.codex_adapter.child_environment',
            return_value={'PATH': '/usr/bin'}), patch(
            'context_learning.synthetic_teacher.shutil.which', return_value='/usr/bin/codex'), patch(
            'context_learning.synthetic_teacher.subprocess.Popen', FakeProcess), patch(
            'context_learning.synthetic_teacher._stop_owned'):
        answer = _invoke_codex(sample, {}, policy, Path(folder), worker,
                               before_launch=lambda: events.append('before'),
                               after_launch=lambda pid: events.append(('after', pid)),
                               launch_failed=lambda: events.append('failed'))
        assert answer['selected_ids'] == []
        assert events == ['before', ('after', 12345)]
    events.clear()
    with patch('context_learning.synthetic_teacher._scope_still_active'), patch(
            'context_learning.synthetic_teacher.codex_adapter.argv', return_value=['codex']), patch(
            'context_learning.synthetic_teacher.codex_adapter.child_environment',
            return_value={'PATH': '/usr/bin'}), patch(
            'context_learning.synthetic_teacher.shutil.which', return_value='/usr/bin/codex'), patch(
            'context_learning.synthetic_teacher.subprocess.Popen',
            side_effect=OSError('local spawn failed')):
        try:
            _invoke_codex(sample, {}, policy, Path(folder), worker,
                          before_launch=lambda: events.append('before'),
                          after_launch=lambda pid: events.append(('after', pid)),
                          launch_failed=lambda: events.append('failed'))
        except OSError:
            pass
        else:
            raise AssertionError('fake spawn unexpectedly succeeded')
        assert events == ['before', 'failed']
