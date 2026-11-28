"""Overview and install views over a real selected install, its registry row and real event rows.

The install is built and selected through the install use cases, registered
the way setup registers it (``registry.api.after_setup``), and its events are
written by ``diagnostics.api.emit`` (the hook writer). Nothing here writes a
registry row or an event line by hand.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from memvane.diagnostics import api as diagnostics
from memvane.registry import api as registry
from memvane.ui import read_adapters
from tests.support.case import FixtureCase


def tree_digest(root):
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob('*')):
        if path.is_file() and not path.is_symlink():
            digest.update(str(path).encode() + path.read_bytes())
    return digest.hexdigest()


class OverviewTests(FixtureCase):
    def setUp(self):
        super().setUp()
        from tests.support.install_builder import build_install
        from tests.support.project import make_project
        self.project = make_project(self.home, name='uiproj', dirname='uiproj')
        self.project.write_note('notes.md', 'Dashboard notes.\n')
        self.project.write_card('concepts/a.md', title='A', body='Alpha.\n')
        self.project.write_card('concepts/b.md', title='B', body='Beta.\n', status='generated')
        self.project.commit()
        self.install = build_install(self, self.project, install_id='c' * 32)
        outcome = registry.after_setup('activate-index-origin', str(self.project.root), str(self.install.state),
                                       {'status': 'selected', 'phase': 'selected'})
        self.assertIn(outcome['status'], ('registered', 'unchanged'), outcome)
        self.state = self.install.state
        self.id = self.install.install_id

    def emit(self, event, **fields):
        self.assertTrue(diagnostics.emit(event, state_root=self.state, install=self.id, host='claude',
                                         session='s' * 32, **fields))

    def test_overview_proves_the_install_and_summarizes_its_last_day(self):
        self.emit('recall', outcome='served', reason='selected', ms=120, count=2,
                  prompt=diagnostics.prompt_fields('secret prompt text'))
        self.emit('recall', outcome='empty', reason='no_candidates', ms=80, notes=['needs_extractor'])
        self.emit('recall.edit', outcome='served', reason='selected', ms=40)
        self.emit('wake.sync', outcome='failed', reason='sync_failed')
        self.emit('index.publish', outcome='published', generation='g1')
        before = tree_digest(self.state)
        seen = []
        view = read_adapters.overview(home=self.home, observe=lambda install, root: seen.append(install))
        self.assertEqual(tree_digest(self.state), before)
        self.assertEqual(view['snapshot']['state'], 'complete')
        self.assertEqual([group['installId'] for group in view['projects']], [self.id])
        self.assertEqual([row['installId'] for row in view['installs']], [self.id])
        self.assertEqual(seen, [self.id])
        main = view['projects'][0]['main']
        self.assertEqual((main['targetId'], main['kind'], main['branch'], main['displayRoot']),
                         (self.id, 'main', 'main', str(self.project.root)))
        self.assertEqual((main['cards']['verified'], main['cards']['generated']), (1, 1))
        inst = view['installs'][0]
        self.assertEqual(inst['recalls24h'], 2)
        self.assertGreaterEqual(inst['errors24h'], 1)
        self.assertIsNotNone(main['lastSyncAt'])
        self.assertIn('needs_extractor', [alert['code'] for alert in view['alerts']])
        self.assertNotIn('secret prompt text', json.dumps(view))

    def test_install_views_need_a_proved_target(self):
        self.emit('recall', outcome='served', reason='selected', ms=100)
        self.emit('recall', outcome='refused', reason='route_unproved', ms=10)
        self.emit('recall.edit', outcome='served', reason='selected', ms=30, degraded='worktree')
        target = read_adapters.resolve_target(self.id, home=self.home)
        self.assertEqual((target['kind'], target['routeState'], target['stateRoot']),
                         ('main', 'candidate', str(self.state)))
        recalls = read_adapters.install_recalls(self.id, home=self.home, limit=2)
        self.assertEqual((recalls['total'], len(recalls['items'])), (3, 2))
        stamps = [row['ts'] for row in recalls['items']]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        main_only = read_adapters.install_recalls(self.id, home=self.home, target=self.id)
        self.assertEqual(main_only['total'], 2)
        served = read_adapters.install_recalls(self.id, home=self.home, outcome='served')
        self.assertEqual(served['total'], 2)
        errors = read_adapters.install_errors(self.id, home=self.home)
        self.assertEqual([row['reason'] for row in errors['items']], ['route_unproved'])
        self.assertNotIn('detail', errors['items'][0])
        summary = read_adapters.install_summary(self.id, home=self.home)
        self.assertEqual(summary['routeState'], 'candidate')
        self.assertIn('hook', [line['line'] for line in summary['slo']])
        self.assertTrue(summary['summary'])
        for bad, code in ((f'{self.id}:{"0" * 12}', 'target_unknown'), ('d' * 32, 'target_unknown'),
                          ('nope', 'target_id_invalid')):
            with self.assertRaises(read_adapters.TargetError) as caught:
                read_adapters.resolve_target(bad, home=self.home)
            self.assertEqual(str(caught.exception), code)
        with self.assertRaises(read_adapters.TargetError):
            read_adapters.install_recalls(self.id, home=self.home, limit=0)
        with self.assertRaises(read_adapters.TargetError):
            read_adapters.install_summary(self.id, home=self.home, hours='-1')

    def test_a_route_that_no_longer_proves_the_install_is_refused(self):
        route = self.project.root / '.memvane' / 'route.json'
        self.assertTrue(route.exists())
        saved = route.read_bytes()
        route.write_bytes(b'{}')
        try:
            with self.assertRaises(read_adapters.TargetError) as caught:
                read_adapters.resolve_target(self.id, home=self.home)
            self.assertEqual((str(caught.exception), caught.exception.status), ('target_unproved', 409))
            view = read_adapters.overview(home=self.home)
            inst = view['installs'][0]
            self.assertEqual((inst['recalls24h'], inst['slo']), (0, 'no-data'))
        finally:
            route.write_bytes(saved)
