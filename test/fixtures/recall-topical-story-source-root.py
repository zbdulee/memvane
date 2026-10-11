"""Synthetic parent/story source-root regression for topical recall."""
import hashlib
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))
import recall
import wiki_freshness
import config as qmd_config
from context_learning import synthetic_experiment, synthetic_scope
import topical_story_scope
from topical_story_scope import WIKI_COLLECTION


def main():
    with tempfile.TemporaryDirectory(prefix='qmd-topical-recall-') as directory:
        parent = Path(directory).resolve()
        story = parent / 'synthetic-story'
        wiki = story / '.auto-context/wiki'
        source = story / '01_Settings/ledger.md'
        source.parent.mkdir(parents=True)
        source.write_text('synthetic source revision\n', encoding='utf-8')
        info = source.stat()
        revision = {'kind': 'file', 'path': '01_Settings/ledger.md',
                    'collection': 'fantasy-20261008-settings',
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                    'size': info.st_size, 'mtimeNs': info.st_mtime_ns}
        card = wiki / 'topical-v2' / ('a' * 24) / 'ledger.md'
        card.parent.mkdir(parents=True)
        card.write_text('---\nstatus: verified\ncreatedBy: qmd-auto-context\n'
                        'sourceRevisions:\n  - '+json.dumps(revision)+'\n'
                        'sources:\n  - {kind: "file", path: "01_Settings/ledger.md"}\n---\n'
                        'Synthetic verified ledger body\n', encoding='utf-8')
        legacy = wiki / 'concepts/legacy.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text('legacy card\n', encoding='utf-8')
        config = {'collectionPaths': {WIKI_COLLECTION: 'synthetic-story/.auto-context/wiki'},
                  'wikiPath': 'synthetic-story/.auto-context/wiki'}
        roots = (parent, wiki)
        item = {'file': f'{WIKI_COLLECTION}/topical-v2/{"a" * 24}/ledger.md',
                '_collection': WIKI_COLLECTION}
        selected_root = recall.topical_source_root(item, config, str(story), roots, story)
        meta = recall.read_wiki_meta(item, config, str(story), roots=roots,
                                     source_opts=(2, []), pending_cutoffs={},
                                     source_root=selected_root)
        fresh = wiki_freshness.check_card(meta, selected_root, [])
        wrong_root = wiki_freshness.check_card(meta, parent, [])
        legacy_root = recall.topical_source_root(
            {'file': f'{WIKI_COLLECTION}/concepts/legacy.md',
             '_collection': WIKI_COLLECTION}, config, str(story), roots, story)
        foreign_root = recall.topical_source_root(
            {'file': item['file'], '_collection': 'foreign-wiki'},
            config, str(story), roots, story)
        no_scope_root = recall.topical_source_root(item, config, str(story), roots, None)
        # A parent source with identical bytes must not rescue a topical hit
        # when the exact-story policy check was denied.
        parent_source = parent / '01_Settings/ledger.md'
        parent_source.parent.mkdir()
        parent_source.write_bytes(source.read_bytes())
        colliding = wiki_freshness.check_card(meta, parent, [])
        denied = dict(item, _wiki_trusted=True, _wiki_source_revisions=[revision])
        protected = recall.story_topical_namespace(denied, story)
        recall.deny_story_topical_result(denied)
        fixture = parent / 'query-fixture.json'
        fixture.write_text(json.dumps({'results': [dict(item, score=1,
            snippet='daemon snippet must not leak')]}), encoding='utf-8')
        os.environ['QMD_QUERY_FIXTURE'] = str(fixture)
        synthetic_scope.NOVEL_ROOT = parent
        synthetic_experiment.story_for = lambda _root: story
        qmd_config.find_project_config = lambda _cwd: {
            'projectRoot': str(parent), 'config': {
                **qmd_config.DEFAULT_CONFIG,
                'collections': [WIKI_COLLECTION],
                'collectionPaths': config['collectionPaths'],
                'collectionRoles': {WIKI_COLLECTION: 'raw'},
                'recallStrategy': 'flat',
                'wikiPath': config['wikiPath'],
                'indexing': True}}
        def denied_policy(_story):
            raise ValueError('synthetic_policy_denied')
        topical_story_scope.read = denied_policy
        recall.sys.stdin = io.StringIO(json.dumps({
            'cwd': str(story), 'prompt': 'synthetic ledger context query'}))
        emitted = io.StringIO()
        with contextlib.redirect_stdout(emitted):
            recall.main()
        denied_output = emitted.getvalue()
        print(json.dumps({'selectedStory': selected_root == story,
                          'trusted': meta['trusted'], 'sourcePresent': meta['sourcePresent'],
                          'fresh': fresh['state'], 'parentFresh': wrong_root['state'],
                          'legacyParent': legacy_root == parent,
                          'foreignParent': foreign_root == parent,
                          'noScopeParent': no_scope_root == parent,
                          'parentCollisionFresh': colliding['state'],
                          'protected': protected,
                          'deniedTrusted': denied['_wiki_trusted'],
                          'deniedRevisionsRemoved': '_wiki_source_revisions' not in denied,
                          'roleDriftOutputEmpty': denied_output == '',
                          'legacyUnprotected': not recall.story_topical_namespace(
                              {'file': f'{WIKI_COLLECTION}/concepts/legacy.md'}, story)}))


if __name__ == '__main__':
    main()
