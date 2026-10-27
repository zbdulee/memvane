import { execFileSync } from 'node:child_process';

// 카드가 **디스크에 있는 그대로** recall 신뢰를 통과하는지와 freshness 상태를 함께 본다.
// "provenance 를 썼다"와 "recall 이 그 카드를 주입한다"는 다른 사실이다 — 라이브에 status
// 만 찍히고 증명 필드가 빠진 카드 27장이 남아 있는 것이 그 간극의 실측이다. 그래서 두
// 단정을 한 번에 얻는 프로브를 테스트 사이에서 공유한다(판정 재구현 금지 — 실제
// `wiki_card.is_auto_trusted_card` / `wiki_freshness.check_card` 를 호출한다).
export function wikiTrustState(projectDir, cardRel) {
  return JSON.parse(execFileSync('python3', ['-c', [
    'import json, sys',
    'from pathlib import Path',
    'sys.path.insert(0, "core")',
    'import wiki_card as R, wiki_compile as wc, wiki_freshness as WF, wiki_source_missing as wsm',
    'root = Path(sys.argv[1]).resolve()',
    'text = (root / sys.argv[2]).read_text(encoding="utf-8")',
    'block = wc.FRONTMATTER_RE.match(text).group(1)',
    'scalars = R.parse_frontmatter_scalars(block)',
    'revisions = R.frontmatter_source_revisions(block)',
    'meta = {"status": scalars.get("status"), "createdBy": scalars.get("createdBy"),',
    '        "sourceRevisions": revisions}',
    'config = json.loads((root / ".auto-context/settings.json").read_text())',
    'print(json.dumps({"trusted": R.is_auto_trusted_card(meta), "status": scalars.get("status"),',
    '                  "revisions": revisions,',
    '                  "freshness": WF.check_card({"sourceRevisions": revisions}, root,',
    '                                             wsm.allow_roots_of(config))}))',
  ].join('\n'), projectDir, cardRel], { cwd: process.cwd(), encoding: 'utf8' }));
}

// 기계 검수 통과를 흉내내지 않고 **실제 유일한 writer**(`wc.stamp_verification`)로 찍는다.
// 갓 컴파일된 카드는 `generated`이므로 신뢰 판정을 관통시키려면 이 전이가 필요하고,
// frontmatter 를 손으로 고치면 status 만 있고 증명 필드가 없는 그 라이브 형태를 테스트가
// 스스로 만들어낸다.
export function stampVerified(projectDir, cardRel, engine = 'codex', mode = 'cross-engine') {
  const out = execFileSync('python3', ['-c', [
    'import sys',
    'from pathlib import Path',
    'sys.path.insert(0, "core")',
    'import wiki_compile as wc',
    'import yaml_scalars as ys',
    'root = Path(sys.argv[1]).resolve()',
    'card = root / sys.argv[2]',
    // 실제 caller(`wiki_verify_worker`)와 같다: 검증기가 읽은 본문의 지문을 넘긴다.
    // 헬퍼가 None 을 넘기면 결속 없는 legacy 카드를 만들어 이 결속을 테스트에서
    // 구조적으로 우회하게 된다.
    'body_hash = ys.card_body_hash(card.read_text(encoding="utf-8"))',
    'print(wc.stamp_verification(card, "verified", sys.argv[3], sys.argv[4], body_hash))',
  ].join('\n'), projectDir, cardRel, engine, mode], { cwd: process.cwd(), encoding: 'utf8' }).trim();
  if (out !== 'True') throw new Error(`stamp_verification failed: ${out}`);
}
