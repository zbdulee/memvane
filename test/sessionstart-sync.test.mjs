import test from 'node:test';
import assert from 'node:assert/strict';
import {execFileSync, spawnSync} from 'node:child_process';
import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {join} from 'node:path';
import {removeTemp, repoTemp} from './helpers/temp.mjs';
import {waitUntilAsync} from './helpers/timing.mjs';

// The fixture's enqueue-then-persist-failure retry output, replayed below as
// the SessionStart sync result update.sh sees.
let retryResult = null;

test('SessionStart detects pull and branch file changes without enabling compile', () => {
  const capture = repoTemp('qmd-sessionstart-retry-out');
  try {
    const retryOut = join(capture, 'retry.json');
    const run = spawnSync('python3', ['test/fixtures/sessionstart-sync-checks.py'], {
      cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
      env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1',
            QMD_SESSIONSTART_RETRY_OUT: retryOut},
    });
    if (run.status !== 0) throw new Error(run.stdout + run.stderr);
    retryResult = JSON.parse(readFileSync(retryOut, 'utf8'));
  } finally {
    removeTemp(capture);
  }
});

test('previous version-1 sync snapshots remain readable', () => {
  const run = spawnSync('python3', ['test/fixtures/sync-v1-compat-checks.py'], {
    cwd: process.cwd(), encoding: 'utf8', timeout: 30000,
    env: {...process.env, QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1'},
  });
  if (run.status !== 0) throw new Error(run.stdout + run.stderr);
});

// Runs update.sh --worker with its SessionStart sync answer fixed to `syncResult`
// and returns the backend manager calls the detached embed child made.
async function managerCallsAfterSync(prefix, syncResult) {
  const work = repoTemp(prefix);
  const bin = join(work, 'bin');
  const fakeHome = join(work, 'fakehome');
  const lock = join(work, 'embed.lock');
  const managerLog = join(work, 'manager.log');
  const hookLog = join(work, 'hook.log');
  const syncJson = join(work, 'sync.json');
  try {
    mkdirSync(join(work, '.auto-context'), {recursive: true});
    mkdirSync(join(work, 'docs'), {recursive: true});
    mkdirSync(bin, {recursive: true});
    for (const fixture of ['prepare-shared-qmd-store.py', 'prepare-synthetic-qmd-runtime.py']) {
      execFileSync('python3', [`test/fixtures/${fixture}`, fakeHome], {
        env: {...process.env, HOME: fakeHome, PYTHONDONTWRITEBYTECODE: '1'},
      });
    }
    writeFileSync(join(work, '.auto-context/settings.json'), JSON.stringify({
      indexing: true, collections: ['x'], collectionPaths: {x: 'docs'},
    }));
    writeFileSync(syncJson, JSON.stringify(syncResult));
    const python = execFileSync('which', ['python3'], {encoding: 'utf8'}).trim();
    writeFileSync(join(bin, 'python3'), [
      '#!/bin/sh',
      'case "$1" in',
      `  */sync.py) cat "${syncJson}"; exit 0 ;;`,
      '  */context_learning_synthetic_reconcile.py) echo \'{"wikiCollectionsQueued":0}\'; exit 0 ;;',
      '  */qmd_route.py) [ "$2" = verify-shared-enrollment ] && exit 0 ;;',
      '  */runtime_work.py) [ "$2 $4" = "qmd embed" ] && { echo "0 chunks"; exit 0; } ;;',
      'esac',
      `exec "${python}" "$@"`,
    ].join('\n'), {mode: 0o755});
    writeFileSync(join(bin, 'qmd'), '#!/bin/sh\nexit 0\n', {mode: 0o755});
    const manager = join(work, 'manager.sh');
    writeFileSync(manager, `#!/bin/sh\necho "$*" >> "${managerLog}"\nexit 0\n`, {mode: 0o755});
    execFileSync('bash', [join(process.cwd(), 'core/update.sh'), '--worker', work], {
      encoding: 'utf8', env: {
        ...process.env, PATH: `${bin}:${process.env.PATH}`, HOME: fakeHome,
        QMD_CACHE_DIR: join(work, 'cache'), QMD_LOCK_BASE: join(work, 'locks'),
        QMD_HOOK_LOG: hookLog, QMD_EMBED_LOCKDIR: lock,
        QMD_BACKEND_MANAGER: manager, QMD_SETUP_GUARD_FIXTURE: '1',
        QMD_SKIP_BACKGROUND_EMBED: '', QMD_SKIP_BACKGROUND_WORKER: '',
        QMD_RECALL_LOG: '',
      },
    });
    assert.equal(await waitUntilAsync(() => existsSync(hookLog) &&
      /EMBED: started in background \(pid=\d+\)/.test(readFileSync(hookLog, 'utf8'))), true,
    'detached embed child did not start');
    const pid = Number(readFileSync(hookLog, 'utf8').match(/EMBED: started in background \(pid=(\d+)\)/)[1]);
    const finished = await waitUntilAsync(() => {
      try { process.kill(pid, 0); return false; } catch { return true; }
    });
    assert.equal(finished, true, 'detached embed child did not exit');
    assert.ok(readFileSync(hookLog, 'utf8').includes(JSON.stringify(syncResult)),
      'embed child must have reached the SessionStart sync step');
    return existsSync(managerLog) ? readFileSync(managerLog, 'utf8').trim().split('\n') : [];
  } finally {
    removeTemp(work);
  }
}

const kicks = (calls) => calls.filter((line) => line.startsWith('kick-wiki-compile '));

test('SessionStart retry of an already-queued revision still kicks wiki compile with queued=0', async () => {
  assert.ok(retryResult, 'fixture retry output missing; the first test must pass first');
  assert.equal(retryResult.compileQueued, 0);
  assert.equal(retryResult.compileWakePending, true);
  const pending = kicks(await managerCallsAfterSync('qmd-sessionstart-wake-pending', retryResult));
  assert.equal(pending.length, 1, pending.join('\n'));
  assert.match(pending[0], / --flush$/);

  // Without the internal signal, queued=0 stays a no-kick (no extra wake).
  const {compileWakePending, ...plain} = retryResult;
  assert.deepEqual(kicks(await managerCallsAfterSync('qmd-sessionstart-wake-none', plain)), []);

  // A truthful queued count keeps the original kick.
  const queued = kicks(await managerCallsAfterSync('qmd-sessionstart-wake-queued',
    {...plain, compileQueued: 1, compileReason: 'queued'}));
  assert.equal(queued.length, 1, queued.join('\n'));
  assert.match(queued[0], / --flush$/);
});
