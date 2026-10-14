import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, chmodSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';

function fixture(script) {
  const root = mkdtempSync(join(tmpdir(), 'qmd-durable-'));
  const docs = join(root, 'docs'); mkdirSync(docs);
  const queue = join(root, 'queue');
  const row = `synthetic\t${docs}\n`;
  writeFileSync(queue, row, {mode: 0o600});
  const qmd = join(root, 'qmd');
  writeFileSync(qmd, `#!/bin/bash\nprintf '%s\\n' "$1" >> '${root}/calls'\n${script}\n`, {mode: 0o700});
  // The restricted test host denies kill-zero even for exited processes.
  // Node wait/spawnSync supplies actual exit receipts for these synthetic
  // children; the test helper never treats EPERM as death or forces removal.
  const exits=join(root,'exited-pids.json');writeFileSync(exits,'[]');
  const python=spawnSync('python3',['-c','import sys;print(sys.executable)'],{encoding:'utf8'}).stdout.trim();
  const proof=join(root,'lock-proof.py');
  writeFileSync(proof,`import json,sys\nfrom pathlib import Path\nsys.path.insert(0,${JSON.stringify(join(process.cwd(),'core'))})\nimport worker_lock as L\nif sys.argv[1]!='reclaim':raise SystemExit(L.main(sys.argv[1:]))\ndead=json.loads(Path(${JSON.stringify(exits)}).read_text())\nrow=L.reclaim(sys.argv[2],alive=lambda pid:False if pid in dead else L.process_alive(pid))\nprint(json.dumps(row))\nraise SystemExit(0 if row['status'] in ('reclaimed','absent') else 75)\n`);
  writeFileSync(join(root,'python3'),`#!/bin/sh\ncase "$1" in */worker_lock.py) shift; exec '${python}' '${proof}' "$@";; esac\nexec '${python}' "$@"\n`,{mode:0o700});
  const env = {...process.env, QMD_DIRTY_QUEUE: queue, QMD_FAKE_QMD: qmd,
    QMD_BIN:qmd,PATH:root+':'+process.env.PATH,
    QMD_CACHE_DIR: join(root, 'cache'), QMD_LOCK_BASE: join(root, 'locks'),
    QMD_INDEX_WORKER_LOCKDIR: join(root, 'worker.lock'),
    QMD_WRITER_LOCKDIR: join(root, 'writer.lock'),
    QMD_EMBED_LOCKDIR: join(root, 'embed.lock'), QMD_NO_RELOAD: '1'};
  return {root, docs, queue, row, qmd, env,exits};
}

function exited(f,pid) {const rows=JSON.parse(readFileSync(f.exits,'utf8'));rows.push(pid);writeFileSync(f.exits,JSON.stringify(rows));}

function run(f) {
  const result=spawnSync('bash', ['backend/index_worker.sh'], {cwd: process.cwd(), env: f.env, encoding: 'utf8'});
  if (result.status!==null) exited(f,result.pid);
  return result;
}

test('add and update errors retain the durable claim and report failure until retry succeeds', () => {
  for (const step of ['collection', 'update']) {
    const f = fixture(`if [ "$1" = ${step} ] && [ ! -e "$RETRY_MARKER" ]; then exit 42; fi\nexit 0`);
    f.env.RETRY_MARKER = join(f.root, 'retry');
    const first = run(f);
    assert.equal(first.status, 1, `${step}: ${first.stderr}`);
    assert.equal(readFileSync(f.queue, 'utf8'), f.row);
    assert.equal(JSON.parse(readFileSync(f.queue + '.claim.json', 'utf8')).phase, 'processing');
    writeFileSync(f.env.RETRY_MARKER, 'ok');
    assert.equal(run(f).status, 0);
    assert.equal(readFileSync(f.queue, 'utf8'), '');
    assert.equal(existsSync(f.queue + '.claim.json'), false);
  }
});

test('unwritable queue directory during embed failure retains work and later retries', () => {
  const f = fixture(`if [ "$1" = embed ] && [ ! -e "$RETRY_MARKER" ]; then chmod 500 "$QUEUE_PARENT"; exit 42; fi\nexit 0`);
  f.env.QUEUE_PARENT = f.root; f.env.RETRY_MARKER = join(f.root, 'retry');
  try {
    const failed = run(f);
    assert.equal(failed.status, 1, failed.stderr);
    assert.equal(readFileSync(f.queue, 'utf8'), f.row);
    assert.equal(JSON.parse(readFileSync(f.queue + '.claim.json', 'utf8')).phase, 'processing');
  } finally { chmodSync(f.root, 0o700); }
  writeFileSync(f.env.RETRY_MARKER, 'ok');
  assert.equal(run(f).status, 0);
  assert.equal(readFileSync(f.queue, 'utf8'), '');
});

test('ack write failure retains the claim and retries after storage recovers', () => {
  const f = fixture(`if [ "$1" = embed ] && [ ! -e "$RETRY_MARKER" ]; then chmod 500 "$QUEUE_PARENT"; fi\nexit 0`);
  f.env.QUEUE_PARENT = f.root; f.env.RETRY_MARKER = join(f.root, 'retry');
  try {
    const failed = run(f);
    assert.equal(failed.status, 1, failed.stderr);
    assert.equal(readFileSync(f.queue, 'utf8'), f.row);
    assert.equal(JSON.parse(readFileSync(f.queue + '.claim.json', 'utf8')).phase, 'processing');
  } finally { chmodSync(f.root, 0o700); }
  writeFileSync(f.env.RETRY_MARKER, 'ok');
  assert.equal(run(f).status, 0);
  assert.equal(readFileSync(f.queue, 'utf8'), '');
});

test('SIGKILL during embed preserves claim and concurrent enqueue for separate retries', async () => {
  const f = fixture(`if [ "$1" = embed ] && [ ! -e "$RETRY_MARKER" ]; then touch "$EMBED_STARTED"; sleep 30; fi\nexit 0`);
  f.env.RETRY_MARKER = join(f.root, 'retry'); f.env.EMBED_STARTED = join(f.root, 'embed-started');
  const child = spawn('bash', ['backend/index_worker.sh'],
    {cwd: process.cwd(), env: f.env, detached: true, stdio: 'ignore'});
  try {
    for (let i = 0; i < 100 && !existsSync(f.env.EMBED_STARTED); i++) await delay(30);
    assert.ok(existsSync(f.env.EMBED_STARTED), 'worker reached embed');
    const next = join(f.root, 'next'); mkdirSync(next);
    const py = spawnSync('python3', ['-c',
      `import sys; sys.path.insert(0,'core'); import dirty_queue; dirty_queue.enqueue_collections({'next': '${next}'})`],
      {cwd: process.cwd(), env: f.env, encoding: 'utf8'});
    assert.equal(py.status, 0, py.stderr);
    assert.match(readFileSync(f.queue, 'utf8'), /next\t/);
    assert.equal(run(f).status, 75, 'a concurrent worker leaves the live/unknown claim alone');
    assert.match(readFileSync(f.queue, 'utf8'), /next\t/);
    process.kill(-child.pid, 'SIGKILL');
    await new Promise(resolve => child.once('exit', resolve));
    exited(f,child.pid);
    writeFileSync(f.env.RETRY_MARKER, 'ok');
    const restarted = run(f);
    assert.equal(restarted.status, 0, restarted.stderr);
    assert.equal(readFileSync(f.queue, 'utf8'), `next\t${next}\n`);
    assert.equal(run(f).status, 0);
    assert.equal(readFileSync(f.queue, 'utf8'), '');
  } finally {
    try { process.kill(-child.pid, 'SIGKILL'); } catch {}
  }
});

test('failed reload survives 0/0 retry, restart, and concurrent enqueue until handoff succeeds', () => {
  const f = fixture(`if [ "$1" = update ]; then echo "Indexed: 0 new, 0 updated, 0 unchanged, 0 removed"; fi
if [ "$1" = embed ]; then
  if [ ! -e "$FIRST_EMBED" ]; then touch "$FIRST_EMBED"; echo "Embedded 1 chunks from 1 documents in 1s";
  else echo "Embedded 0 chunks from 0 documents in 0s"; fi
fi
exit 0`);
  f.env.FIRST_EMBED = join(f.root, 'first-embed');
  delete f.env.QMD_NO_RELOAD;
  const allow = join(f.root, 'allow-reload');
  const calls = join(f.root, 'reload-calls');
  const manager = join(f.root, 'manager');
  writeFileSync(manager, `#!/bin/bash\n[ "$1" = reload ] || exit 1\necho reload >> "$RELOAD_CALLS"\n[ -e "$ALLOW_RELOAD" ]\n`, {mode: 0o700});
  f.env.QMD_BACKEND_MANAGER = manager;
  f.env.RELOAD_CALLS = calls;
  f.env.ALLOW_RELOAD = allow;
  for (let attempt = 1; attempt <= 2; attempt++) {
    const failed = run(f);
    assert.equal(failed.status, 1, failed.stderr);
    assert.equal(JSON.parse(readFileSync(f.queue + '.claim.json', 'utf8')).reloadRequired, true);
    assert.match(readFileSync(f.queue, 'utf8'), /^synthetic\t/);
    assert.equal(readFileSync(calls, 'utf8').trim().split('\n').length, attempt);
  }
  const next = join(f.root, 'next'); mkdirSync(next);
  const py = spawnSync('python3', ['-c',
    `import sys; sys.path.insert(0,'core'); import dirty_queue; dirty_queue.enqueue_collections({'next': '${next}'})`],
    {cwd: process.cwd(), env: f.env, encoding: 'utf8'});
  assert.equal(py.status, 0, py.stderr);
  writeFileSync(allow, 'ok');
  const resumed = run(f);
  assert.equal(resumed.status, 0, resumed.stderr);
  assert.equal(readFileSync(calls, 'utf8').trim().split('\n').length, 3);
  assert.equal(readFileSync(f.queue, 'utf8'), `next\t${next}\n`);
  assert.equal(existsSync(f.queue + '.claim.json'), false);
  assert.equal(run(f).status, 0);
  assert.equal(readFileSync(f.queue, 'utf8'), '');
});
