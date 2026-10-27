import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp, repoTemp } from './helpers/temp.mjs';

test('local trial marker blocks automatic compile and propagates the block to SessionStart', () => {
  const root = repoTemp('qmd-local-trial-gate');
  try {
    mkdirSync(join(root, 'hooks'), { recursive: true });
    writeFileSync(join(root, '.qmd-local-auto-compile-disabled'), 'local trial\n');
    const marker = join(root, 'auto-compile-called');
    const update = join(root, 'update.sh');
    writeFileSync(update, `#!/usr/bin/env bash\n[ "\${QMD_AUTO_COMPILE_DISABLED:-}" = 1 ] || touch "${marker}"\n`, { mode: 0o755 });
    const manager = join(root, 'manager.sh');
    writeFileSync(manager, '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });
    const env = { ...process.env, PLUGIN_ROOT: root,
      QMD_CORE_UPDATE_SCRIPT: update, QMD_BACKEND_MANAGER: manager,
      QMD_TOPICAL_SANDBOX_ROOT: '', QMD_CACHE_DIR: join(root, 'cache'), QMD_RECALL_LOG: '' };
    execFileSync('bash', ['hooks/run-hook', 'compile', 'codex'], {
      cwd: process.cwd(), env, input: JSON.stringify({ cwd: root }), encoding: 'utf8',
    });
    execFileSync('bash', ['hooks/run-hook', 'update', 'codex'], {
      cwd: process.cwd(), env, input: JSON.stringify({ cwd: root }), encoding: 'utf8',
    });
    assert.equal(existsSync(marker), false);
  } finally { removeTemp(root); }
});

test('manager does not start an existing compile queue with local trial block', () => {
  const root = repoTemp('qmd-local-trial-manager');
  try {
    const worker = join(root, 'worker.sh');
    const marker = join(root, 'worker-called');
    writeFileSync(worker, `#!/usr/bin/env bash\ntouch "${marker}"\n`, { mode: 0o755 });
    execFileSync('bash', ['core/backend_manager.sh', 'kick-wiki-compile', root, '--flush'], {
      cwd: process.cwd(), encoding: 'utf8',
      env: { ...process.env, QMD_AUTO_COMPILE_DISABLED: '1',
        QMD_BACKEND_STATE_DIR: join(root, 'state'), QMD_BACKEND_LOG: join(root, 'manager.log'),
        QMD_DAEMON_LOG: join(root, 'daemon.log'), QMD_COMPILE_WORKER_SCRIPT: worker },
    });
    assert.equal(existsSync(marker), false);
  } finally { removeTemp(root); }
});

test('local runtime file scopes QMD paths to the plugin process', () => {
  const root = repoTemp('qmd-local-runtime-env');
  try {
    const capture = join(root, 'capture.txt');
    const update = join(root, 'update.sh');
    writeFileSync(join(root, '.qmd-local-runtime.env'), [
      `export QMD_BIN="${root}/private-bin/qmd"`,
      `export QMD_DIRTY_QUEUE="${root}/private-queue"`,
      `export INDEX_PATH="${root}/private-index.sqlite"`,
      'export QMD_DAEMON_PORT=18483',
    ].join('\n') + '\n');
    writeFileSync(update, `#!/usr/bin/env bash\nprintf '%s\\n' "$QMD_BIN" "$QMD_DIRTY_QUEUE" "$INDEX_PATH" "$QMD_DAEMON_PORT" > "${capture}"\n`, { mode: 0o755 });
    const manager = join(root, 'manager.sh');
    writeFileSync(manager, '#!/usr/bin/env bash\nexit 0\n', { mode: 0o755 });
    // A neutral cwd: a checkout of this repository can itself be a selected Laya project.
    execFileSync('bash', [join(process.cwd(), 'hooks/run-hook'), 'update', 'codex'], {
      cwd: tmpdir(), encoding: 'utf8', input: '{}',
      env: { ...process.env, PLUGIN_ROOT: root, QMD_CORE_UPDATE_SCRIPT: update,
        QMD_BACKEND_MANAGER: manager, QMD_CACHE_DIR: join(root, 'cache'), QMD_RECALL_LOG: '' },
    });
    assert.equal(readFileSync(capture, 'utf8'), [
      `${root}/private-bin/qmd`, `${root}/private-queue`, `${root}/private-index.sqlite`, '18483', '',
    ].join('\n'));
  } finally { removeTemp(root); }
});

test('sandbox guard runs before a hostile local runtime file', () => {
  const root = repoTemp('qmd-local-runtime-sandbox-guard');
  try {
    const marker = join(root, 'executed');
    writeFileSync(join(root, '.qmd-local-runtime.env'), `touch "${marker}"\n`, { mode: 0o600 });
    const out = execFileSync('bash', ['hooks/run-hook', 'topical-stop', 'codex'], {
      cwd: process.cwd(), encoding: 'utf8',
      env: { ...process.env, PLUGIN_ROOT: root, QMD_SANDBOX: '1', QMD_TOPICAL_SANDBOX_ROOT: root },
    });
    assert.equal(out, '{}\n');
    assert.equal(existsSync(marker), false);
  } finally { removeTemp(root); }
});

test('local runtime parser rejects shell commands without executing them', () => {
  const root = repoTemp('qmd-local-runtime-data-only');
  try {
    const marker = join(root, 'executed');
    const update = join(root, 'update.sh');
    writeFileSync(update, `#!/usr/bin/env bash\ntouch "${marker}"\n`, { mode: 0o755 });
    writeFileSync(join(root, '.qmd-local-runtime.env'),
      `export QMD_BIN="$(touch ${marker})"\n`, { mode: 0o600 });
    // A neutral cwd: a checkout of this repository can itself be a selected Laya project.
    const out = execFileSync('bash', [join(process.cwd(), 'hooks/run-hook'), 'update', 'codex'], {
      cwd: tmpdir(), encoding: 'utf8',
      env: { ...process.env, PLUGIN_ROOT: root, QMD_CORE_UPDATE_SCRIPT: update,
        QMD_TOPICAL_SANDBOX_ROOT: '', QMD_RECALL_LOG: '' },
    });
    assert.equal(out, '');
    assert.equal(existsSync(marker), false);
  } finally { removeTemp(root); }
});
