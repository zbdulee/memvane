import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {spawnSync} from 'node:child_process';

const python = process.env.STRUCTURE_TEST_PYTHON || 'python3';
const helper = resolve('tests/structure/guard_rules.py');
const guard = resolve('test/structure/v1_guard.py');

function env(home) {
  return {PATH: process.env.PATH, HOME: home, TMPDIR: home, PYTHONDONTWRITEBYTECODE: '1',
    QMD_SETUP_GUARD_FIXTURE: '1'};
}

function scan(t, files) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), 'mv-guard-rules-')));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  for (const [path, text] of Object.entries(files)) {
    mkdirSync(dirname(join(root, path)), {recursive: true});
    writeFileSync(join(root, path), text);
  }
  const result = spawnSync(python, ['-B', helper, root], {encoding: 'utf8', env: env(root), timeout: 60000});
  assert.equal(result.error, undefined);
  assert.equal(result.signal, null, 'guard_rules.py timed out');
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
  return JSON.parse(result.stdout);
}

// "<rule>:<file>:<line>:<target>:<kind>:<name>" -> "<file>:<target>:<kind>:<name>" for one rule.
function rows(report, rule) {
  return report.findings.filter(x => x.startsWith(rule + ':'))
    .map(x => x.split(':').slice(1)).map(([file, , target, kind, ...name]) => [file, target, kind, name.join(':')].join(':'))
    .sort();
}

const journalApi = [
  'from __future__ import annotations',
  '',
  'class _Ops:',
  '    def __getattr__(self, name):',
  '        return name',
  '',
  '_reader = _Ops()',
  '',
  'def open_store(path: str):',
  '    return path',
  '',
  'def __getattr__(name):',
  '    return name',
  ''].join('\n');

test('strict G1 flags private names, private attributes and proxies reached through another domain api', t => {
  const report = scan(t, {
    'memvane/journal/api.py': journalApi,
    'memvane/journal/inner.py': 'from memvane.journal.api import _reader\nvalue = _reader._state\n',
    'memvane/sync/worker.py': [
      'from memvane.journal.api import _reader',
      'from memvane.journal import api as journal_api',
      'import memvane.journal.api as japi2',
      'from memvane.journal.api import hidden_name',
      '',
      'def a():',
      '    from memvane.journal.api import open_store',
      '    store = open_store("p")',
      '    with open_store("q") as handle:',
      '        handle._lock',
      '    return store._session, store.public, store._replace',
      '',
      'def b(store):',
      '    return store._session',
      '',
      'def c():',
      '    return journal_api._reader.anything, journal_api.missing, japi2.open_store',
      '',
      'class Holder:',
      '    def __init__(self):',
      '        from memvane.journal.api import open_store',
      '        self._fn = open_store',
      '        self._fn._bound',
      '',
      'def d():',
      '    for row in journal_api.open_store("r"):',
      '        row._raw',
      '    first, second = journal_api.open_store("s")',
      '    return second._tail',
      ''].join('\n'),
  });
  const file = 'memvane/sync/worker.py';
  assert.deepEqual(rows(report, 'G1'), [
    `${file}:journal:getattr_forward:hidden_name`,
    `${file}:journal:getattr_forward:missing`,
    `${file}:journal:import_private:_reader`,
    `${file}:journal:private_attr:_reader`,
    `${file}:journal:private_attr:open_store()._lock`,
    `${file}:journal:private_attr:open_store()._raw`,
    `${file}:journal:private_attr:open_store()._session`,
    `${file}:journal:private_attr:open_store()._tail`,
    `${file}:journal:proxy_attr:_reader.anything`,
  ].sort());
});

test('strict G5 counts private module names and rejects api passages', t => {
  const filler = n => Array.from({length: n}, (_, i) => `_v${i} = ${i}`).join('\n') + '\n';
  const report = scan(t, {
    'memvane/journal/api.py': journalApi,
    'memvane/store/api.py': [
      'from __future__ import annotations',
      'from importlib import import_module',
      '',
      'def explicit(path: str, *, mode: str = "r") -> str:',
      '    return path',
      '',
      'def forward(*args, **kwargs):',
      '    return explicit(*args, **kwargs)',
      '',
      'def only_kwargs(**kwargs):',
      '    return kwargs',
      '',
      'class _Bundle:',
      '    def method(self, *args, **kwargs):',
      '        return args',
      '    def named(self, value):',
      '        return value',
      '    lam = staticmethod(lambda *a, **k: a)',
      '',
      'bundle = _Bundle()',
      '_private_one = 1',
      ''].join('\n'),
    'memvane/registry/api.py': filler(31),
    'memvane/route/api.py': 'from __future__ import annotations\n__all__ = ()\n' + filler(30),
  });
  assert.deepEqual(rows(report, 'G5'), [
    'memvane/journal/api.py:journal:module_getattr:__getattr__',
    'memvane/journal/api.py:journal:proxy_class:_Ops',
    'memvane/registry/api.py:registry:public_attributes:31',
    'memvane/store/api.py:store:dynamic_import:import_module',
    'memvane/store/api.py:store:varargs_forwarder:_Bundle.lam',
    'memvane/store/api.py:store:varargs_forwarder:_Bundle.method',
    'memvane/store/api.py:store:varargs_forwarder:forward',
    'memvane/store/api.py:store:varargs_forwarder:only_kwargs',
  ]);
  assert.equal(report.metrics.api_attributes['memvane/store/api.py'], 10);
  assert.equal(report.metrics.api_attributes['memvane/route/api.py'], 30);
});

test('strict G9 flags SQL naming a table created by another domain', t => {
  const report = scan(t, {
    'memvane/runtime/schema.py': "DDL = 'CREATE TABLE IF NOT EXISTS wakes (key TEXT)'\n" +
      "OWN = 'DELETE FROM wakes WHERE key = ?'\n",
    'memvane/store/schema.py': "DDL = ['CREATE TABLE store_meta (k TEXT)',\n" +
      "       'CREATE VIRTUAL TABLE IF NOT EXISTS fts_x USING fts5(body)']\n",
    'memvane/install/cancel.py': [
      '"""Remove rows FROM wakes in a docstring is ignored."""',
      'def run(db, table):',
      "    db.execute('DELETE FROM wakes WHERE key = ?')",
      "    db.execute('SELECT value FROM \"store_meta\" LIMIT 1')",
      "    db.execute(f'SELECT * FROM {table}')",
      "    db.execute('SELECT * FROM unknown_table')",
      "    db.execute('UPDATE wakes SET key = 1')",
      "    db.execute('SELECT a FROM x JOIN fts_x ON 1')",
      "    db.execute('INSERT INTO wakes(key) VALUES (?)')",
      "    message = 'copied from wakes into the log'",
      ''].join('\n'),
  });
  const file = 'memvane/install/cancel.py';
  assert.deepEqual(rows(report, 'G9'), [
    `${file}:runtime:delete from:wakes`,
    `${file}:runtime:into:wakes`,
    `${file}:runtime:update:wakes`,
    `${file}:store:from:store_meta`,
    `${file}:store:join:fts_x`,
  ]);
  assert.deepEqual(report.metrics.table_owners, {fts_x: ['store'], store_meta: ['store'], wakes: ['runtime']});
});

test('strict G1 limits api handles to the members their types.py Protocol declares', t => {
  const report = scan(t, {
    'memvane/store/api.py': 'def open_store(path, *, reader=False):\n    return path\n',
    'memvane/store/types.py': [
      'from typing import Protocol',
      '',
      'class ViewStoreHandle(Protocol):',
      '    def head(self, handle): ...',
      '    def search(self, pin, limit, *, work=5): ...',
      '    def close(self, extra=None): ...',
      ''].join('\n'),
    'memvane/store/view/view_store.py': [
      'from memvane.store.view.mixin import ReaderMixin',
      '',
      'class ViewStore(ReaderMixin):',
      '    def head(self, handle):',
      '        return handle',
      '    def close(self):',
      '        return None',
      ''].join('\n'),
    'memvane/store/view/mixin.py': [
      'DEFAULT_WORK = 5',
      '',
      'class ReaderMixin:',
      '    def search(self, pin, limit, *, work=DEFAULT_WORK):',
      '        return pin',
      ''].join('\n'),
    'memvane/journal/api.py': 'def pin_schema3(record):\n    return record\n',
    'memvane/sync/worker.py': [
      'from memvane.store import api as store_api',
      'from memvane.journal.api import pin_schema3',
      '',
      'def a():',
      '    store = store_api.open_store("p", reader=True)',
      '    store.head(1)',
      '    store.verify()',
      '    store._session',
      '    store.head(1).anything',
      '    return store.close',
      '',
      'def b(record):',
      '    with pin_schema3(record) as (store, handle, pin):',
      '        store.search(pin, 3)',
      '        store.vectors(1)',
      '        return handle.view_id, pin.gen_id, pin._token',
      ''].join('\n'),
  });
  const file = 'memvane/sync/worker.py';
  assert.deepEqual(rows(report, 'G1'), [
    `${file}:journal:private_attr:pin_schema3()._token`,
    `${file}:journal:undeclared_handle_member:pin_schema3().vectors`,
    `${file}:store:private_attr:open_store()._session`,
    `${file}:store:undeclared_handle_member:open_store().verify`,
    // close repeats no signature: the class method has no ``extra``; search's
    // default names a module constant whose value the Protocol repeats.
    'memvane/store/types.py:store:handle_signature:ViewStoreHandle.close',
  ].sort());
});

test('strict G1 treats the journal writer scope as a handle with declared members', t => {
  const report = scan(t, {
    'memvane/journal/api.py': [
      'def build_writer_scope(decision, *, queue_owner):',
      '    return decision',
      '',
      'def selected_v1_writer_scope(decision, *, queue_owner):',
      '    return decision',
      ''].join('\n'),
    'memvane/journal/types.py': [
      'from typing import Protocol',
      '',
      'class WriterHandle(Protocol):',
      '    def admission_proof(self): ...',
      '    def store(self): ...',
      '    def mark_new_write(self): ...',
      '    def validate(self, *, deadline=None): ...',
      '',
      'class WriterToken:',
      '    def admission_proof(self):',
      '        return 1',
      '    def store(self):',
      '        return 2',
      '    def mark_new_write(self):',
      '        return 3',
      '    def validate(self, *, deadline=5):',
      '        return 4',
      ''].join('\n'),
    'memvane/sync/worker.py': [
      'from memvane.journal import api as journal_api',
      'from memvane.journal.api import build_writer_scope',
      '',
      'def a(decision, owner):',
      '    with journal_api.selected_v1_writer_scope(decision, queue_owner=owner) as writer:',
      '        writer.validate()',
      '        writer.mark_new_write()',
      '        writer.forge_proof()',
      '        return writer._context',
      '',
      'def b(decision, owner):',
      '    with build_writer_scope(decision, queue_owner=owner) as writer:',
      '        return writer.admission_proof(), writer.store(), writer.lifetime',
      ''].join('\n'),
  });
  const file = 'memvane/sync/worker.py';
  assert.deepEqual(rows(report, 'G1'), [
    `${file}:journal:private_attr:selected_v1_writer_scope()._context`,
    `${file}:journal:undeclared_handle_member:build_writer_scope().lifetime`,
    `${file}:journal:undeclared_handle_member:selected_v1_writer_scope().forge_proof`,
    // validate's default differs from WriterToken's.
    'memvane/journal/types.py:journal:handle_signature:WriterHandle.validate',
  ].sort());
});

test('strict G1 limits reader values to the fields their types.py dataclass or slots declare', t => {
  const report = scan(t, {
    'memvane/journal/api.py': [
      'def resolve_endpoint(project, *, action, host_root):',
      '    return project',
      '',
      'def reader_selection(endpoint):',
      '    return endpoint',
      ''].join('\n'),
    'memvane/journal/types.py': [
      'from dataclasses import dataclass',
      'from typing import Protocol',
      '',
      'class WriterHandle(Protocol):',
      '    pass',
      '',
      '@dataclass(frozen=True, eq=False, init=False)',
      'class VerifiedEndpoint:',
      '    endpoint_id: str',
      '    record: dict',
      '',
      '@dataclass(frozen=True, eq=False, init=False)',
      'class ReaderSelection:',
      '    epoch: int',
      '    preferred_endpoint: str',
      ''].join('\n'),
    'memvane/recall/api.py': 'def query_endpoint(endpoint, *, checkout):\n    return endpoint\n',
    'memvane/recall/types.py': [
      'class ReaderResult:',
      "    __slots__ = ('data_generation', 'rows')",
      ''].join('\n'),
    'memvane/wiki/compile/retrieval.py': [
      'from memvane.journal import api as lease',
      'from memvane.recall import api as dispatch',
      '',
      'def a(project):',
      '    with lease.resolve_endpoint(project, action="recall", host_root=None) as endpoint:',
      '        endpoint.record.get("store_format")',
      '        endpoint.raw_bytes',
      '        selected = lease.reader_selection(endpoint)',
      '        result = dispatch.query_endpoint(endpoint, checkout=project)',
      '    return (selected.epoch, selected.preferred_endpoint, selected.transition,',
      '            result.rows, result.data_generation, result.cursor)',
      ''].join('\n'),
  });
  const file = 'memvane/wiki/compile/retrieval.py';
  assert.deepEqual(rows(report, 'G1'), [
    `${file}:journal:undeclared_value_member:reader_selection().transition`,
    `${file}:journal:undeclared_value_member:resolve_endpoint().raw_bytes`,
    `${file}:recall:undeclared_value_member:query_endpoint().cursor`,
  ].sort());
});

test('strict G1 follows a handle passed as an argument into the same module', t => {
  const report = scan(t, {
    'memvane/store/api.py': 'def open_store(path, *, reader=False):\n    return path\n',
    'memvane/store/types.py': [
      'from typing import Protocol',
      '',
      'class ViewStoreHandle(Protocol):',
      '    def head(self, handle): ...',
      ''].join('\n'),
    'memvane/store/view/view_store.py': [
      'class ViewStore:',
      '    def head(self, handle):',
      '        return handle',
      ''].join('\n'),
    'memvane/sync/worker.py': [
      'from memvane.store import api as store_api',
      'from memvane.store.types import ViewStoreHandle',
      '',
      'def run(path, rows):',
      '    store = store_api.open_store(path)',
      '    _publish(store, 1)',
      '    _keyword(path, handle=store)',
      '    _spread(*rows, store)',
      '    return store',
      '',
      'def _publish(store, n):',
      '    store.head(n)',
      '    store.verify()',
      '    _chained(store)',
      '    return store._session',
      '',
      'def _chained(target):',
      '    return target.rewind',
      '',
      'def _keyword(path, *, handle):',
      '    return handle.seal',
      '',
      'def _spread(first, second):',
      '    return second.untracked',
      '',
      'def _annotated(store: ViewStoreHandle):',
      '    return store.head(1), store.compact',
      '',
      'def _plain(store):',
      '    return store.anything',
      ''].join('\n'),
  });
  const file = 'memvane/sync/worker.py';
  assert.deepEqual(rows(report, 'G1'), [
    `${file}:store:private_attr:open_store()._session`,
    `${file}:store:undeclared_handle_member:ViewStoreHandle.compact`,
    `${file}:store:undeclared_handle_member:open_store().rewind`,
    `${file}:store:undeclared_handle_member:open_store().seal`,
    `${file}:store:undeclared_handle_member:open_store().verify`,
  ].sort());
});

test('strict G9 excuses only the listed read of another domain table', t => {
  const store = "DDL = 'CREATE TABLE store_meta (k TEXT)'\n";
  const report = scan(t, {
    'memvane/store/schema.py': store,
    'memvane/route/index_host_route.py': [
      "READ = 'SELECT incarnation,scope_id FROM store_meta WHERE singleton=1'",
      "WRITE = 'DELETE FROM store_meta'",
      "COPY = 'INSERT INTO log SELECT k FROM store_meta'",
      ''].join('\n'),
    'memvane/route/other.py': "READ = 'SELECT k FROM store_meta'\n",
  });
  assert.deepEqual(rows(report, 'G9'), [
    'memvane/route/index_host_route.py:store:delete from:store_meta',
    'memvane/route/index_host_route.py:store:from:store_meta',
    'memvane/route/other.py:store:from:store_meta',
  ]);
  const unused = scan(t, {
    'memvane/store/schema.py': store,
    'memvane/route/index_host_route.py': "OTHER = 'SELECT 1'\n",
  });
  assert.deepEqual(rows(unused, 'G9'), ['memvane/route/index_host_route.py:store:unused_read_exception:store_meta']);
});

test('the product guard reports strict rules separately and filters them by rule and domain', t => {
  const home = realpathSync(mkdtempSync(join(tmpdir(), 'mv-guard-cli-')));
  t.after(() => rmSync(home, {recursive: true, force: true}));
  const run = (...args) => {
    const result = spawnSync(python, ['-B', guard, ...args], {encoding: 'utf8', env: env(home), timeout: 300000});
    assert.equal(result.error, undefined);
    assert.equal(result.signal, null, 'v1_guard.py timed out');
    return {status: result.status, report: JSON.parse(result.stdout)};
  };
  const {status, report} = run('--rules', 'G9', '--domain', 'runtime');
  assert.equal(status, 0, 'strict findings do not fail without --strict');
  assert.deepEqual(Object.keys(report.counts), []);
  assert.deepEqual(Object.keys(report.strict_counts), ['G9']);
  assert.ok(Object.keys(report.strict_by_domain).every(d => d === 'runtime'));
  assert.ok(report.strict_issues.G9.every(x => x.split(':')[2] === 'runtime'), report.strict_issues.G9.join('\n'));
  assert.equal(report.strict_counts.G9, report.strict_issues.G9.length);
  const strict = run('--rules', 'G9', '--domain', 'runtime', '--strict');
  assert.equal(strict.status, report.strict_counts.G9 ? 1 : 0);
});
