import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';

function run(py) {
  return execFileSync('python3', ['-c', py], { cwd: process.cwd(), encoding: 'utf8' });
}

test('builtin_engines filters to known engines and stores symbolic names only', () => {
  const py = `import json,sys; sys.path.insert(0,'core'); import wiki_compile_defaults as d
print(json.dumps([d.builtin_engines(('claude','bogus','hermes')), d.builtin_engines(('bogus',))]))`;
  const out = JSON.parse(run(py));
  assert.deepEqual(out[0], ['claude', 'hermes']);
  assert.deepEqual(out[1], ['claude', 'codex', 'hermes']);
});

test('parse_engines filters to known engines, empty = all', () => {
  const py = `import json,sys; sys.path.insert(0,'core'); import wiki_compile_defaults as d
print(json.dumps([list(d.parse_engines('codex,bogus')), list(d.parse_engines('')), list(d.parse_engines(None))]))`;
  const [filtered, empty, none] = JSON.parse(run(py));
  assert.deepEqual(filtered, ['codex']);
  assert.deepEqual(empty, ['claude', 'codex', 'hermes']);
  assert.deepEqual(none, ['claude', 'codex', 'hermes']);
});

// 생성기가 **의도하는** 값은 full_compile_block()이 SSOT다. 파일로 나가는 것은
// compile_block()의 delta이므로, 의도는 여기서 보고 delta 여부는 아래 테스트가 본다.
test('full_compile_block has post_tool_source trigger, portable builtins (no dispatch gate), batch', () => {
  const py = `import json,sys; sys.path.insert(0,'core')
import wiki_compile_defaults as d, wiki_compile_worker as wcw
b=d.full_compile_block('/PR'); print(json.dumps({
 'mode':b['mode'],
 'trig':'post_tool_source' in b['triggers'],
 'backends':b['extractor']['backends'],
 'builtins':b['extractor']['builtins'],
 # dispatch 게이트를 대신하는 불변식: 이 블록만으로 세 엔진이 argv로 해석된다.
 'resolved':{e: wcw.resolve_extractor_argv(b, e) for e in ('claude','codex','hermes')},
 'serialized':json.dumps(b), 'reasoningEffort':b['reasoningEffort'],
   'cooldown':b['extractor']['cooldownSeconds'],'batch':b['batch']}))`;
  const b = JSON.parse(run(py));
  // 활성화 스위치는 mode 하나다 (enabled/autoWrite는 스키마에서 제거됐다).
  assert.equal(b.mode, 'auto-wiki');
  assert.equal(b.trig, true);
  for (const engine of ['claude', 'codex', 'hermes']) {
    assert.ok(Array.isArray(b.resolved[engine]) && b.resolved[engine].length === 2,
      `${engine} must resolve to an adapter argv without any dispatch key`);
    assert.match(b.resolved[engine][1], new RegExp(`core/extractors/${engine}_adapter\\.py$`));
  }
  assert.deepEqual(b.backends, {});
  assert.deepEqual(b.builtins, ['claude', 'codex', 'hermes']);
  assert.doesNotMatch(b.serialized, /\/PR|core\/extractors|_adapter\.py/);
  assert.equal(b.cooldown, 600);
  assert.deepEqual(b.batch, { idleSeconds: 90, maxItems: 5 });
  assert.deepEqual(b.reasoningEffort, {
    generation: 'low', verify: 'low', semanticDedup: 'medium', engines: {},
  });
});

test('emitted inference policy follows runtime defaults without sharing mutable nested values', () => {
  const out = JSON.parse(run(`import json,sys; sys.path.insert(0,'core')
import config as c, wiki_compile_defaults as d
first=d.full_compile_block('/PR')
same = first['reasoningEffort'] == c.DEFAULT_CONFIG['compile']['reasoningEffort'] and first['verify'] == c.DEFAULT_CONFIG['compile']['verify']
first['reasoningEffort']['engines']['codex'] = {'verify':'high'}
first['verify']['builtins'].append('other')
again=d.full_compile_block('/PR')
print(json.dumps({'same':same,'effort':again['reasoningEffort'],'verify':again['verify']}))`));
  assert.equal(out.same, true);
  assert.deepEqual(out.effort, { generation: 'low', verify: 'low', semanticDedup: 'medium', engines: {} });
  assert.equal(out.verify.crossEngine, 'off');
  assert.deepEqual(out.verify.builtins, []);
});

// delta-only 계약: 생성기가 **생략한** 키는 전부 DEFAULT_CONFIG와 값이 같아야 하고,
// **남긴** 키는 전부 기본값과 다르거나 기본값에 아예 없는 키여야 한다. 한쪽이라도 어긋나면
// 생성기가 조용히 동작을 바꾸는 것이므로, effective 동결 테스트보다 먼저 여기서 깨진다.
test('compile_block은 delta다 — 생략한 키는 기본값과 같고, 남긴 키는 기본값과 다르다', () => {
  const py = `import json,sys; sys.path.insert(0,'core')
import wiki_compile_defaults as d, config as c
full=d.full_compile_block('/PR'); delta=d.compile_block('/PR'); dflt=c.DEFAULT_CONFIG['compile']

def walk(f, dl, df, path=''):
    bad_omitted, bad_kept = [], []
    for k, v in f.items():
        p = f'{path}.{k}' if path else k
        if isinstance(v, dict) and isinstance(df.get(k), dict):
            sub = walk(v, dl.get(k, {}), df[k], p)
            bad_omitted += sub[0]; bad_kept += sub[1]
            continue
        if k not in dl:
            # 생략됐다 → 기본값과 같아야 한다
            if k not in df or not d.same_as_default(v, df[k]):
                bad_omitted.append(p)
        else:
            # 남았다 → 기본값과 다르거나, 기본값에 없는 키여야 한다
            if k in df and d.same_as_default(v, df[k]):
                bad_kept.append(p)
    return bad_omitted, bad_kept

omitted_wrong, kept_wrong = walk(full, delta, dflt)
print(json.dumps({'omittedWrong': omitted_wrong, 'keptWrong': kept_wrong,
                  'deltaKeys': sorted(delta), 'omitted': d.default_valued_compile_keys('/PR')}))`;
  const r = JSON.parse(run(py));
  assert.deepEqual(r.omittedWrong, [], '기본값과 다른데 생략된 키 (effective가 조용히 바뀐다)');
  assert.deepEqual(r.keptWrong, [], '기본값과 같은데 남은 키 (delta-only 위반)');
  // 기본값과 다르다는 이유로 반드시 남아야 하는 것들.
  assert.deepEqual(r.deltaKeys, ['extractor', 'mode', 'triggers']);
  // dispatch 게이트가 사라진 뒤 extractor delta에 남아야 하는 것은 builtins 하나뿐이고,
  // 그것만으로 엔진이 해석돼야 한다(게이트 키를 빠뜨려 죽는 경로가 더는 없다).
  const ext = JSON.parse(run(`import json,sys; sys.path.insert(0,'core')
import wiki_compile_defaults as d, wiki_compile_worker as wcw
delta=d.compile_block('/PR')
print(json.dumps({'keys':sorted(delta['extractor']),
                  'resolved':wcw.resolve_extractor_argv(delta,'claude') is not None}))`));
  assert.deepEqual(ext.keys, ['builtins']);
  assert.equal(ext.resolved, true);
  assert.ok(r.omitted.includes('verify') && r.omitted.includes('batch'));
});

test('compile_block --engines codex limits portable builtins', () => {
  const py = `import json,sys; sys.path.insert(0,'core'); import wiki_compile_defaults as d
b=d.compile_block('/PR', d.parse_engines('codex'))
print(json.dumps({'extractor':b['extractor'],
                  'fullBackends':d.full_compile_block('/PR', d.parse_engines('codex'))['extractor']['backends']}))`;
  const r = JSON.parse(run(py));
  assert.deepEqual(r.extractor.builtins, ['codex']);
  // delta-only: backends는 기본값 {}과 같아 emit되지 않지만 의도값은 여전히 {}이다.
  assert.deepEqual(Object.keys(r.extractor), ['builtins']);
  assert.deepEqual(r.fullBackends, {});
});
