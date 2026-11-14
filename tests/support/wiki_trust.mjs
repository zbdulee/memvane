// Whether a card as it is on disk passes recall trust, and its freshness (S5 L0 replacement
// of test/helpers/wiki_trust.mjs). The decision is the product's own (`wiki.card.api`
// is_auto_trusted_card / check_card), never re-implemented here; the project config is
// the minimal `.memvane/settings.json` expanded through `settings.api`.
//
// The old helper's `stampVerified` is not carried over: the only verification writer
// (`wiki.verify` stamp) has no api entry yet, and a hand-written stamp would make the
// unproved card shape this probe exists to catch. Python tests build verified cards
// with `tests/support/project.py` (`write_card`).
import { execFileSync } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

export function wikiTrustState(projectDir, cardRel) {
  return JSON.parse(execFileSync('python3', ['-B', '-c', [
    'import json, sys',
    'from pathlib import Path',
    'from memvane.settings import api as settings',
    'from memvane.wiki.card import api as card',
    'from memvane.wiki.card.types import FRONTMATTER_RE',
    'root = Path(sys.argv[1]).resolve()',
    'text = (root / sys.argv[2]).read_text(encoding="utf-8")',
    'block = FRONTMATTER_RE.match(text).group(1)',
    'scalars = card.parse_frontmatter_scalars(block)',
    'revisions = card.frontmatter_source_revisions(block)',
    'meta = {"status": scalars.get("status"), "createdBy": scalars.get("createdBy"),',
    '        "sourceRevisions": revisions}',
    'config = settings.load_project_config(str(root))',
    'print(json.dumps({"trusted": card.is_auto_trusted_card(meta), "status": scalars.get("status"),',
    '                  "revisions": revisions,',
    '                  "freshness": card.check_card({"sourceRevisions": revisions}, root,',
    '                                               card.allow_roots_of(config))}))',
  ].join('\n'), projectDir, cardRel], { cwd: ROOT, encoding: 'utf8',
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' } }));
}
