import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { chmodSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync, symlinkSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { tmpdir } from 'node:os';

const CORE = resolve(import.meta.dirname, '../core');

function python(code, args = []) {
  return new Promise((resolveRun, reject) => {
    const child = spawn('python3', ['-c', code, ...args], {
      env: { ...process.env, PYTHONPATH: CORE, PYTHONDONTWRITEBYTECODE: '1', QMD_RECALL_LOG: '' },
    });
    let stdout = '';
    let stderr = '';
    child.stdout.setEncoding('utf8').on('data', chunk => { stdout += chunk; });
    child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
    child.on('error', reject);
    child.on('close', code => code === 0 ? resolveRun(stdout.trim())
      : reject(new Error(`python exited ${code}: ${stderr}`)));
  });
}

function privateDir(t) {
  const dir = realpathSync(mkdtempSync(join(tmpdir(), 'qmd-job-telemetry-')));
  chmodSync(dir, 0o700);
  t.after(() => rmSync(dir, { recursive: true, force: true }));
  return dir;
}

test('concurrent writers append complete private JSONL lines without content fields', async t => {
  const dir = privateDir(t);
  const path = join(dir, 'job-telemetry.jsonl');
  const script = `
from job_telemetry import emit
import sys
path, worker = sys.argv[1:]
print(all(emit(path, 'queued', job_id=f'job:{worker}:{i}', episode=i+1,
               op='writing', parent_id='episode:1', prompt='SECRET_PROMPT',
               body='SECRET_BODY') for i in range(12)))
`;
  const results = await Promise.all(Array.from({ length: 5 }, (_, i) => python(script, [path, String(i)])));
  assert.deepEqual(results, Array(5).fill('True'));
  const raw = readFileSync(path, 'utf8');
  const rows = raw.trimEnd().split('\n').map(JSON.parse);
  assert.equal(rows.length, 60);
  assert.equal(new Set(rows.map(row => row.job_id)).size, 60);
  assert.ok(rows.every(row => row.schema === 'qmd-job-telemetry-v1'));
  assert.doesNotMatch(raw, /SECRET_PROMPT|SECRET_BODY|"prompt"|"body"/);
  assert.equal(statSync(path).mode & 0o777, 0o600);
});

test('malformed and unavailable metrics become null; local process aliases are normalized', async t => {
  const dir = privateDir(t);
  const path = join(dir, 'job-telemetry.jsonl');
  const result = await python(`
from job_telemetry import emit
import sys
path = sys.argv[1]
print(emit(path, 'finished', job_id='sample:1', op='train', status='success',
    resources={'avg_cpu_percent':'42', 'peak_cpu_percent':float('nan'),
               'observed_peak_rss_kib':1024, 'resource_samples':0,
               'secret':'DO_NOT_LOG'}, secret='DO_NOT_LOG'))
print(emit(path, 'finished', job_id='sample:2', op='train', status='success',
    resources={'observed_avg_cpu_percent':175, 'observed_peak_cpu_percent':220,
               'observed_avg_rss_kib':2048, 'observed_peak_rss_kib':4096,
               'resource_samples':3, 'elapsed_seconds':1.25,
               'before':{'available_mib':8000,'swap_used_mib':100,
                         'memory_pressure':{'system_free_percent':36},
                         'gpu':'mps-possible'},
               'after':{'available_mib':7500,'swap_used_mib':110,
                        'gpu_usage_percent':None}}))
`, [path]);
  assert.equal(result, 'True\nTrue');
  const raw = readFileSync(path, 'utf8');
  const [unknown, measured] = raw.trimEnd().split('\n').map(JSON.parse);
  for (const key of ['cpu_avg_percent', 'cpu_peak_percent', 'rss_avg_mib', 'rss_peak_mib']) {
    assert.equal(unknown[key], null);
  }
  assert.equal(unknown.resource_samples, 0);
  assert.equal(measured.cpu_avg_percent, 175);
  assert.equal(measured.cpu_peak_percent, 220); // 100% means one core, not a cap.
  assert.equal(measured.rss_avg_mib, 2);
  assert.equal(measured.rss_peak_mib, 4);
  assert.equal(measured.run_ms, 1250);
  assert.equal(measured.system_before.available_mib, 8000);
  assert.equal(measured.system_before.memory_free_percent, 36);
  assert.equal(measured.system_before.gpu_usage_percent, null);
  assert.equal(measured.system_after.swap_used_mib, 110);
  assert.doesNotMatch(raw, /DO_NOT_LOG|"secret"/);
});

test('failed local trainer keeps its original error, exit code, elapsed time and sampled resources', async t => {
  const dir = privateDir(t);
  const script = join(dir, 'fail.py');
  writeFileSync(script, 'import time, sys\ntime.sleep(0.35)\nsys.exit(17)\n');
  const path = join(dir, 'job-telemetry.jsonl');
  const output = await python(`
from context_learning.local_cycle import LocalTrainer
from job_telemetry import emit, replay
from pathlib import Path
import json, sys
root, script = Path(sys.argv[1]), sys.argv[2]
trainer = LocalTrainer([sys.executable, script])
try:
    trainer.call('smoke', {'schema_version':1}, root/'result.json', cwd=root)
except ValueError as exc:
    reason = str(exc)
else:
    raise AssertionError('expected subprocess failure')
emit(root/'job-telemetry.jsonl', 'finished', job_id='trainer:failed',
     op='laya_train', status='error', error_code='train_failed',
     exit_code=trainer.last_exit_code, resources=trainer.last_resources)
row = replay((root/'job-telemetry.jsonl').read_text().splitlines())[0]
print(json.dumps({'reason':reason,'exit_code':row['exit_code'],
                  'run_ms':row['run_ms'],'samples':row['resource_samples'],
                  'rss_peak_mib':row['rss_peak_mib']}))
`, [dir, script]);
  const row = JSON.parse(output);
  assert.equal(row.reason, 'trainer_failed');
  assert.equal(row.exit_code, 17);
  assert.ok(row.run_ms >= 300);
  assert.ok(row.samples > 0 || row.samples === null);
  if (row.samples === null) assert.equal(row.rss_peak_mib, null);
  else assert.ok(row.rss_peak_mib > 0);
  assert.doesNotMatch(readFileSync(path, 'utf8'), /fail.py|smoke-request/);
});

test('detached worker records a failed cycle and a pre-lock exception as errors', async t => {
  const dir = privateDir(t);
  const path = join(dir, 'job-telemetry.jsonl');
  const result = await python(`
from context_learning.shared import run
from job_telemetry import emit, replay
from pathlib import Path
from unittest.mock import patch
import json, os, sys, time
root = Path(sys.argv[1]); path = root / 'job-telemetry.jsonl'
for number, outcome in ((1, 'failed'), (2, 'raise')):
    os.environ['QMD_JOB_ID'] = f'worker:{number}'
    os.environ['QMD_JOB_QUEUED_AT'] = str(time.time() - 0.1)
    emit(path, 'queued', job_id=f'worker:{number}', op='shared_worker')
    with patch('context_learning.shared._policy', return_value={}):
        if outcome == 'raise':
            with patch('context_learning.shared._run_impl', side_effect=ValueError('early')):
                try: run(root)
                except ValueError: pass
        else:
            with patch('context_learning.shared._run_impl', return_value={'status': outcome}):
                run(root)
print(json.dumps([(r['job_id'], r['status'], r['queue_wait_ms'], r['run_ms'])
                  for r in replay(path.read_text().splitlines())]))
`, [dir]);
  const rows = JSON.parse(result);
  assert.deepEqual(rows.map(row => row.slice(0, 2)), [
    ['worker:1', 'error'], ['worker:2', 'error'],
  ]);
  assert.ok(rows.every(row => row[2] >= 0 && row[3] >= 0));
  assert.equal(readFileSync(path, 'utf8').trim().split('\n').length, 6);
});

test('unsafe destination and logging failure return false without touching another file', async t => {
  const dir = privateDir(t);
  const victim = join(dir, 'victim');
  const link = join(dir, 'job-telemetry.jsonl');
  writeFileSync(victim, 'unchanged');
  symlinkSync(victim, link);
  const publicDir = join(dir, 'public');
  const { mkdirSync } = await import('node:fs');
  mkdirSync(publicDir, { mode: 0o755 });
  chmodSync(publicDir, 0o755);
  const result = await python(`
from job_telemetry import emit
import sys
print(emit(sys.argv[1], 'queued', job_id='safe:1'))
print(emit(sys.argv[2], 'queued', job_id='safe:2'))
print(emit(sys.argv[3], 'queued', job_id='bad/identifier'))
`, [link, join(publicDir, 'job-telemetry.jsonl'), join(dir, 'missing', 'job-telemetry.jsonl')]);
  assert.equal(result, 'False\nFalse\nFalse');
  assert.equal(readFileSync(victim, 'utf8'), 'unchanged');
});

test('replay separates attempts, derives durations, and leaves crashed jobs incomplete', async t => {
  const dir = privateDir(t);
  const path = join(dir, 'job-telemetry.jsonl');
  const output = await python(`
from job_telemetry import emit, replay
from pathlib import Path
import json, sys
path = sys.argv[1]
emit(path, 'queued', job_id='episode:7:write', parent_id='episode:7', attempt=1,
     episode=7, op='writing', model_version='cli-v1',
     source_version='base', destination_version='shared-abc', ts=1000)
emit(path, 'started', job_id='episode:7:write', attempt=1, ts=1001)
emit(path, 'finished', job_id='episode:7:write', attempt=1, status='error',
     error_code='provider_quota', ts=1003)
emit(path, 'queued', job_id='episode:7:write', attempt=2, episode=7, ts=1004)
emit(path, 'started', job_id='episode:7:write', attempt=2, ts=1005)
emit(path, 'finished', job_id='episode:7:write', attempt=2, status='success',
     run_ms=900, ts=1006)
emit(path, 'queued', job_id='episode:8:index', ts=1010)
emit(path, 'started', job_id='episode:8:index', ts=1011)
emit(path, 'queued', job_id='episode:9:index', ts=1020)
emit(path, 'started', job_id='episode:9:index', ts=1019)
print(json.dumps(replay(Path(path).read_text().splitlines() + ['not-json'])))
`, [path]);
  const rows = JSON.parse(output);
  assert.equal(rows.length, 4);
  assert.deepEqual(rows.slice(0, 2).map(row => [row.attempt, row.retry_count, row.status,
    row.queue_wait_ms, row.run_ms]), [
    [1, 0, 'error', 1000, 2000],
    [2, 1, 'success', 1000, 900],
  ]);
  assert.equal(rows[0].parent_id, 'episode:7');
  assert.equal(rows[0].source_version, 'base');
  assert.equal(rows[0].destination_version, 'shared-abc');
  assert.equal(rows[0].error_code, 'provider_quota');
  assert.equal(rows[2].status, 'incomplete');
  assert.equal(rows[2].run_ms, null);
  assert.equal(rows[3].queue_wait_ms, null); // Clock skew is unknown, not zero.
});

test('replay rejects malformed finish rows before mutating an attempt', async t => {
  const dir = privateDir(t);
  const path = join(dir, 'job-telemetry.jsonl');
  const output = await python(`
from job_telemetry import emit, replay, SCHEMA
from pathlib import Path
import json, sys
path = sys.argv[1]
emit(path, 'queued', job_id='crashed:1', ts=10)
emit(path, 'started', job_id='crashed:1', ts=11)
lines = Path(path).read_text().splitlines()
bad_status = {'schema':SCHEMA, 'phase':'finished', 'job_id':'crashed:1',
              'attempt':1, 'ts':12, 'status':[], 'run_ms':1000}
bad_metric = {'schema':SCHEMA, 'phase':'finished', 'job_id':'crashed:1',
              'attempt':1, 'ts':13, 'status':'success', 'rss_peak_mib':'invalid'}
bad_exit = {'schema':SCHEMA, 'phase':'finished', 'job_id':'crashed:1',
            'attempt':1, 'ts':13.5, 'status':'error', 'exit_code':'17'}
phantom = {'schema':SCHEMA, 'phase':'finished', 'job_id':'phantom:1',
           'attempt':1, 'ts':14, 'status':[]}
print(json.dumps(replay(lines + [json.dumps(row) for row in
    (bad_status, bad_metric, bad_exit, phantom)])))
`, [path]);
  const rows = JSON.parse(output);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].job_id, 'crashed:1');
  assert.equal(rows[0].status, 'incomplete');
  assert.equal(rows[0].finished_at, null);
  assert.equal(rows[0].run_ms, null);
});
