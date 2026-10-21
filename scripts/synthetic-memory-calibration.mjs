// Explicit model-test guard entry point. Never discovered by npm test.
import { test } from 'node:test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

test('calibrate exact local synthetic forward memory once', { timeout: 420000 }, () => {
  const { QMD_SYNTHETIC_SETUP_PYTHON: python,
    QMD_SYNTHETIC_PROJECT: project,
    QMD_SYNTHETIC_ROOT: root,
    QMD_DEVELOPMENT_GUARD_RUN: run,
    QMD_DEVELOPMENT_GUARD_NONCE: nonce } = process.env;
  if (![python, project, root, run, nonce].every(Boolean) ||
      ![python, project, root, run].every(path.isAbsolute)) {
    throw new Error('synthetic_calibration_explicit_paths_and_model_guard_required');
  }
  execFileSync(python, [fileURLToPath(new URL('../core/project_learning_setup.py', import.meta.url)),
    'calibrate-synthetic', project, root], {
    encoding: 'utf8', timeout: 390000, stdio: ['ignore', 'pipe', 'pipe'],
    env: { ...process.env, QMD_SETUP_GUARD_FIXTURE: '' },
  });
});
