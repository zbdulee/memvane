"""Onboarding -> recall with the fixture provider (model-free stand-in).

Uses the same 1.0 setup use cases as the PR8 end-to-end checks (plan,
prepare, initial build passes, finalize, activate) with a synthetic provider
and lexical recall, for a project with no cards and one with thirty.
"""
from __future__ import annotations

from contextlib import ExitStack
import json
import os
from pathlib import Path
import pwd
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wiki_db_provenance as base  # noqa: E402

TOPICS = ['heron', 'lantern', 'glacier', 'violin', 'saffron', 'compass', 'orchard', 'quartz',
          'harbor', 'falcon', 'meadow', 'ember', 'cobalt', 'willow', 'canyon', 'pepper',
          'beacon', 'tundra', 'marble', 'thistle', 'anchor', 'nebula', 'juniper', 'copper',
          'lagoon', 'raven', 'cedar', 'prism', 'dune', 'mosaic']


class FixtureOnboarding(unittest.TestCase):
    git = base.WikiDbProvenance.git
    revision = base.WikiDbProvenance.revision
    patch_host = base.WikiDbProvenance.patch_host
    onboard = base.WikiDbProvenance.onboard
    run_worker = base.WikiDbProvenance.run_worker
    db = base.WikiDbProvenance.db

    def install(self, cards):
        home = Path(os.environ.get('HOME', '')).resolve()
        account = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
        if (home == account or account in home.parents
                or os.environ.get('QMD_SETUP_GUARD_FIXTURE') != '1'
                or not os.environ.get('MEMVANE_TEST_RUNTIME_ROOT')):
            self.fail('run through onboarding-fixture.test.mjs: temporary HOME and fixture guards required')
        from memvane.settings import config as settings_config
        from memvane.wiki.card import api as card
        self.home = home
        self.project = home / 'project'
        self.project.mkdir(mode=0o700)
        self.git('init', '-q')
        self.raw_name, _ = settings_config.minimal_collection_names(base.SETTINGS_NAME)
        folder = self.project / '.memvane'
        wiki = folder / 'wiki' / 'concepts'
        wiki.mkdir(parents=True)
        (folder / 'settings.json').write_text(json.dumps({'name': base.SETTINGS_NAME}) + '\n')
        note = self.project / 'notes.md'
        note.write_text('Field notes: the emerald heron nests beside the reed pond.\n')
        line = card.dump_source_revisions([self.revision(note)])[0]
        for topic in TOPICS[:cards]:
            title = topic.title() + ' protocol'
            text = (f'---\ntitle: "{title}"\nstatus: verified\ncreatedBy: memvane\n'
                    f'sourceRevisions:\n  - {line}\n---\n# {title}\n'
                    f'The {topic} protocol keeps marker token zq{topic}x for recall checks.\n')
            text = text.replace('createdBy: memvane\n', 'createdBy: memvane\nverifiedBodyHash: '
                                + card.card_body_hash(text) + '\n', 1)
            (wiki / f'{topic}.md').write_text(text)
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')
        self.state = home.joinpath('.local', 'state', 'memvane', 'v1', 'installs', base.INSTALL_ID)
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.patch_host(stack)
        self.onboard()

    def recall(self, prompt):
        from memvane.host.api import serve_selected
        from memvane.journal.api import select_route
        decision = select_route(str(self.project), action='recall')
        self.assertEqual(decision.get('state'), 'candidate', decision)
        served = serve_selected('recall', 'claude', decision, {
            'session_id': 's5-fixture', 'cwd': str(self.project), 'hook_event_name': 'UserPromptSubmit',
            'prompt': prompt})
        return ((served or {}).get('hookSpecificOutput') or {}).get('additionalContext', '')

    def test_project_without_cards_onboards_and_recall_adds_no_card(self):
        self.install(0)
        context = self.recall('emerald heron reed pond')
        self.assertNotIn('zq', context)

    def test_project_with_thirty_cards_recalls_the_matching_card(self):
        self.install(30)
        context = self.recall('what marker does the glacier protocol keep?')
        self.assertIn('zqglacierx', context)
        self.assertNotIn('zqmosaicx', context)


if __name__ == '__main__':
    unittest.main()
