import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, 'core')
from runtime_admission import PreSendDeferred, provider_run
import qa_model_budget

with tempfile.TemporaryDirectory() as tmp:
    ledger = Path(tmp) / 'calls.log'
    qa_model_budget.initialize(ledger, limit=6, used=4)
    os.environ['QMD_QA_CALL_LEDGER'] = str(ledger)
    os.environ['QMD_QA_CALL_LIMIT'] = '6'
    command = [sys.executable, '-c', 'print("synthetic")']
    # The QA launcher reserves its outer host turn before exec and exports the
    # same ledger to a detached provider worker. The caller cannot forget a
    # separate manual reserve step when it uses this entry point.
    host = subprocess.run([sys.executable, 'core/qa_model_budget.py', 'run',
        str(ledger), '6', '--', sys.executable, '-c', '''
import os
assert os.environ['QMD_QA_CALL_LEDGER']
assert os.environ['QMD_QA_CALL_LIMIT'] == '6'
print('host')
'''], text=True, capture_output=True)
    assert host.returncode == 0 and host.stdout.strip() == 'host', host.stderr
    assert qa_model_budget.status(ledger, 6) == 5
    worker = subprocess.run([sys.executable, '-c', '''
import sys
sys.path.insert(0, 'core')
from runtime_admission import provider_run
result = provider_run([sys.executable, '-c', 'print("synthetic")'],
                      text=True, capture_output=True, timeout=5)
assert result.returncode == 0 and result.stdout.strip() == 'synthetic'
'''], text=True, capture_output=True, env=os.environ.copy())
    assert worker.returncode == 0, worker.stdout + worker.stderr
    assert qa_model_budget.status(ledger, 6) == 6
    try:
        provider_run(command, text=True, capture_output=True, timeout=5)
    except PreSendDeferred as exc:
        assert str(exc) == 'qa_call_budget_exhausted'
    else:
        raise AssertionError('a further CLI launch must be denied before Popen')
    assert qa_model_budget.status(ledger, 6) == 6
    try:
        qa_model_budget.initialize(ledger, limit=6, used=0)
    except FileExistsError:
        pass
    else:
        raise AssertionError('a new process must not reset an existing ledger')
    # A fresh worker cannot reset or increase the durable per-project ceiling.
    child = subprocess.run([sys.executable, '-c', '''
import sys
sys.path.insert(0, 'core')
from runtime_admission import PreSendDeferred, provider_run
try:
    provider_run([sys.executable, '-c', 'print("should not run")'], timeout=5)
except PreSendDeferred as exc:
    assert str(exc) == 'qa_call_budget_exhausted'
else:
    raise AssertionError('budget reset across processes')
'''], text=True, capture_output=True, env=os.environ.copy())
    assert child.returncode == 0, child.stdout + child.stderr
    os.environ['QMD_QA_CALL_LIMIT'] = '7'
    try:
        provider_run(command, timeout=5)
    except PreSendDeferred as exc:
        assert str(exc) == 'qa_call_budget_invalid'
    else:
        raise AssertionError('environment limit change must not raise the ceiling')
    assert qa_model_budget.status(ledger, 6) == 6
    host = subprocess.run([sys.executable, 'core/qa_model_budget.py', 'run',
        str(ledger), '6', '--', sys.executable, '-c', 'print("should not run")'],
        text=True, capture_output=True)
    assert host.returncode == 75 and 'qa_call_budget_exhausted' in host.stderr
    assert host.stdout == ''
    # Refuse a partial opt-in rather than silently running without a cap.
    os.environ.pop('QMD_QA_CALL_LIMIT')
    try:
        provider_run(command, timeout=5)
    except PreSendDeferred as exc:
        assert str(exc) == 'qa_call_budget_invalid'
    else:
        raise AssertionError('missing limit must fail closed')
    os.environ.pop('QMD_QA_CALL_LEDGER')
    plain = provider_run(command, text=True, capture_output=True, timeout=5)
    assert plain.returncode == 0 and plain.stdout.strip() == 'synthetic'

    race = Path(tmp) / 'race.log'
    init = subprocess.run([sys.executable, 'core/qa_model_budget.py', 'init',
                           str(race), '1', '--used', '0'], text=True,
                          capture_output=True)
    assert init.returncode == 0, init.stdout + init.stderr
    race_env = {**os.environ, 'QMD_QA_CALL_LEDGER': str(race), 'QMD_QA_CALL_LIMIT': '1'}
    reserve = '''
import sys
sys.path.insert(0, 'core')
import qa_model_budget
from resource_deferred import ResourceDeferred
try:
    qa_model_budget.reserve_from_env()
except ResourceDeferred as exc:
    assert str(exc) == 'qa_call_budget_exhausted'
    raise SystemExit(75)
'''
    contenders = [subprocess.Popen([sys.executable, '-c', reserve], env=race_env,
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(2)]
    outcomes = [child.wait(timeout=5) for child in contenders]
    assert sorted(outcomes) == [0, 75], outcomes
    assert qa_model_budget.status(race, 1) == 1

    broken = Path(tmp) / 'broken.log'
    qa_model_budget.initialize(broken, limit=2, used=0)
    with broken.open('ab') as output:
        output.write(b'R')  # interrupted reservation must fail closed
    os.environ['QMD_QA_CALL_LEDGER'] = str(broken)
    os.environ['QMD_QA_CALL_LIMIT'] = '2'
    try:
        provider_run(command, timeout=5)
    except PreSendDeferred as exc:
        assert str(exc) == 'qa_call_budget_invalid'
    else:
        raise AssertionError('partial ledger must never reset the count')
print(json.dumps({'pass': True, 'externalModelCalls': 0}))
