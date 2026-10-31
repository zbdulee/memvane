import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { removeTemp } from './helpers/temp.mjs';

const OTHER_ACTIONS = ['recall', 'update', 'gate', 'posttool', 'index', 'compile', 'file-change-event'];

function makeHome() {
  return realpathSync(mkdtempSync(join(tmpdir(), 'memvane-onboarding-choice-')));
}

function makeProject(home, name, { settings = true } = {}) {
  const project = join(home, name);
  mkdirSync(join(project, '.memvane'), { recursive: true });
  if (settings) {
    writeFileSync(join(project, '.memvane', 'settings.json'), JSON.stringify({ name, skipPaths: [] }));
  }
  return project;
}

function env(home) {
  return { ...process.env, HOME: home, QMD_SANDBOX: '', CODEX_SANDBOX: '', CLAUDE_SANDBOX: '',
    CLAUDE_HEADLESS: '', QMD_RECALL_LOG: '', PYTHONDONTWRITEBYTECODE: '1' };
}

function hook(action, engine, home, project, session) {
  const payload = { hook_event_name: 'SessionStart', cwd: project, prompt: 'x' };
  if (session) payload.session_id = session;
  return spawnSync('bash', ['hooks/run-hook', action, engine], {
    input: JSON.stringify(payload), encoding: 'utf8', timeout: 30000, env: env(home),
  });
}

function sessionStart(home, project, session) {
  const result = hook('update-queued', 'claude', home, project, session);
  assert.equal(result.status, 0, result.stderr);
  return result.stdout;
}

function choice(home, action, project, ...extra) {
  const result = spawnSync('python3', ['-B', 'core/setup_entry.py', action, '--project', project, ...extra], {
    encoding: 'utf8', timeout: 30000, env: env(home),
  });
  let body = null;
  try { body = JSON.parse(result.stdout); } catch { body = null; }
  return { ...result, body };
}

function choiceFile(home, project) {
  const key = createHash('sha256').update(realpathSync(project)).digest('hex');
  return join(home, '.local', 'state', 'memvane', 'onboarding', `${key}.json`);
}

test('"never" silences the onboarding notice for that project in every later session', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    const before = sessionStart(home, project, 'sess-1');
    assert.match(before, /setup skill로 온보딩/);

    const never = choice(home, 'onboarding-never', project);
    assert.equal(never.status, 0, never.stdout + never.stderr);
    assert.equal(never.body.status, 'onboarding_notice_never');

    assert.equal(sessionStart(home, project, 'sess-1'), '');
    assert.equal(sessionStart(home, project, 'sess-2'), '');
    assert.equal(sessionStart(home, project, undefined), '');
    const codex = hook('update-queued', 'codex', home, project, 'sess-3');
    assert.equal(codex.status, 0, codex.stderr);
    assert.equal(codex.stdout, '');
    assert.equal(choice(home, 'onboarding-status', project).body.choice, 'never');
  } finally {
    removeTemp(home);
  }
});

test('"skip" silences only the session that asked for it', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    const line = sessionStart(home, project, 'sess-1');
    // The notice names the session so the agent can record "skip this time".
    assert.match(line, /onboarding-<skip\|never> --project /);
    assert.match(line, /skip=이번 세션만, never=이 프로젝트에서 다시 묻지 않기/);
    assert.ok(line.includes('sess-1'), line);
    assert.ok(line.split('\n').filter(Boolean).length === 1, line);

    const skip = choice(home, 'onboarding-skip', project, '--session', 'sess-1');
    assert.equal(skip.status, 0, skip.stdout + skip.stderr);
    assert.equal(skip.body.status, 'onboarding_notice_skipped');

    // Resume/compact of the same session stays quiet; a new session is asked again.
    assert.equal(sessionStart(home, project, 'sess-1'), '');
    assert.match(sessionStart(home, project, 'sess-2'), /setup skill로 온보딩/);
    assert.match(sessionStart(home, project, undefined), /setup skill로 온보딩/);
  } finally {
    removeTemp(home);
  }
});

test('"skip" needs a session id and never turns into "never"', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    const line = sessionStart(home, project, undefined);
    assert.match(line, /setup skill로 온보딩/);
    assert.match(line, /onboarding-never --project /);
    assert.doesNotMatch(line, /skip/);
    const skip = choice(home, 'onboarding-skip', project);
    assert.equal(skip.status, 1);
    assert.equal(skip.body.reason, 'onboarding_skip_session_required');
    const bad = choice(home, 'onboarding-skip', project, '--session', 'a b;rm');
    assert.equal(bad.status, 1);
    assert.equal(bad.body.reason, 'onboarding_skip_session_required');
    assert.equal(existsSync(choiceFile(home, project)), false);
    assert.match(sessionStart(home, project, 'sess-9'), /setup skill로 온보딩/);
  } finally {
    removeTemp(home);
  }
});

test('a declined project does not change the notice of another project', () => {
  const home = makeHome();
  try {
    const alpha = makeProject(home, 'alpha');
    const beta = makeProject(home, 'beta');
    assert.equal(choice(home, 'onboarding-never', alpha).status, 0);
    assert.equal(choice(home, 'onboarding-skip', join(home, 'alpha'), '--session', 'sess-1').status, 0);
    assert.equal(sessionStart(home, alpha, 'sess-1'), '');
    const other = sessionStart(home, beta, 'sess-1');
    assert.match(other, /setup skill로 온보딩/);
    assert.ok(other.includes(beta), other);
  } finally {
    removeTemp(home);
  }
});

test('an explicit onboarding request re-enables the notice', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    assert.equal(choice(home, 'onboarding-never', project).status, 0);
    assert.equal(sessionStart(home, project, 'sess-1'), '');
    const resume = choice(home, 'onboarding-resume', project);
    assert.equal(resume.status, 0, resume.stdout + resume.stderr);
    assert.equal(resume.body.status, 'onboarding_notice_enabled');
    assert.match(sessionStart(home, project, 'sess-1'), /setup skill로 온보딩/);
    assert.equal(choice(home, 'onboarding-status', project).body.choice, 'ask');
    // Resuming a project that never declined is a no-op success.
    assert.equal(choice(home, 'onboarding-resume', project).status, 0);
  } finally {
    removeTemp(home);
  }
});

test('declining never selects a route or creates index, wiki or learning state', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    assert.equal(choice(home, 'onboarding-skip', project, '--session', 'sess-1').status, 0);
    assert.equal(choice(home, 'onboarding-never', project).status, 0);

    const classify = spawnSync('python3', ['-B', 'core/laya_index_host_route.py', 'classify', 'update-queued', project],
      { encoding: 'utf8', env: env(home) });
    assert.equal(classify.stdout.trim(), 'legacy', classify.stderr);
    for (const action of OTHER_ACTIONS) {
      const result = hook(action, 'claude', home, project, 'sess-1');
      assert.equal(result.status, 0, `${action}: ${result.stderr}`);
      assert.equal(result.stdout, '', action);
      assert.equal(result.stderr, '', action);
    }
    // Only the choice file exists; the project folder is untouched.
    assert.deepEqual(readdirSync(join(project, '.memvane')), ['settings.json']);
    assert.deepEqual(readdirSync(home).sort(), ['.local', 'alpha']);
    assert.deepEqual(readdirSync(join(home, '.local', 'state', 'memvane')), ['onboarding']);
    const file = choiceFile(home, project);
    assert.deepEqual(readdirSync(join(home, '.local', 'state', 'memvane', 'onboarding')),
      [file.split('/').pop()]);
    const record = JSON.parse(readFileSync(file, 'utf8'));
    assert.equal(record.projectRoot, realpathSync(project));
    assert.equal(record.never, true);
  } finally {
    removeTemp(home);
  }
});

test('a folder without settings stays silent and cannot record a choice', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'plain', { settings: false });
    assert.equal(sessionStart(home, project, 'sess-1'), '');
    const never = choice(home, 'onboarding-never', project);
    assert.equal(never.status, 1);
    assert.equal(never.body.reason, 'onboarding_settings_required');
    assert.deepEqual(readdirSync(home), ['plain']);
  } finally {
    removeTemp(home);
  }
});

test('an unreadable choice record keeps the notice instead of hiding it', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    const file = choiceFile(home, project);
    mkdirSync(join(file, '..'), { recursive: true, mode: 0o700 });
    writeFileSync(file, '{not json', { mode: 0o600 });
    assert.match(sessionStart(home, project, 'sess-1'), /setup skill로 온보딩/);
  } finally {
    removeTemp(home);
  }
});

test('a recorded "never" choice under the state root silences SessionStart', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, 'alpha');
    const file = choiceFile(home, project);
    mkdirSync(join(file, '..'), { recursive: true, mode: 0o700 });
    writeFileSync(file, JSON.stringify({ schema: 'memvane-onboarding-choice-v1',
      projectRoot: realpathSync(project), never: true, skippedSessions: [] }), { mode: 0o600 });
    assert.equal(sessionStart(home, project, 'sess-1'), '');
  } finally {
    removeTemp(home);
  }
});

test('the command the notice names records the answer as written, even with spaces in the path', () => {
  const home = makeHome();
  try {
    const project = makeProject(home, "my project's");
    const line = sessionStart(home, project, 'sess-1');
    const command = line.match(/`([^`]+)`/)[1];
    for (const [answer, session, expected] of [['skip', 'sess-1', ''], ['never', 'sess-2', '']]) {
      const run = spawnSync('bash', ['-c', command.replace('<skip|never>', answer)],
        { encoding: 'utf8', env: env(home) });
      assert.equal(run.status, 0, run.stdout + run.stderr);
      assert.equal(sessionStart(home, project, session), expected);
    }
  } finally {
    removeTemp(home);
  }
});
