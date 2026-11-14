"""External edit routing through two selected synthetic 1.0 installations."""
import hashlib
from unittest.mock import patch

from memvane.registry import api as registry
from memvane.runtime import api as runtime
from memvane.wiki.intake import external_edits as external
from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class ExternalInstallRouting(FixtureCase):
    def setUp(self):
        super().setUp()
        self.installs = []
        for index, ident in enumerate(('4' * 32, '5' * 32)):
            project = make_project(self.home, name='fixture%d' % index, dirname='project%d' % index)
            project.write_note('docs/note.md', 'initial %d\n' % index)
            project.git('add', '--', '.memvane/settings.json', 'docs/note.md')
            project.git('commit', '-qm', 'fixture')
            install = build_install(self, project, install_id=ident)
            self.installs.append(install)
            registration = registry.after_setup('activate-index-origin', str(project.root),
                                                str(install.state), {'phase': 'selected', 'status': 'selected'})
            self.assertIsInstance(registration, dict)
            self.assertNotIn(registration.get('status'), ('unproved', 'skipped', 'unavailable'))
        for _ in range(8):
            if registry.reconcile(max_rows=64, seconds=60.0, backfill_page=64)['complete']:
                break
        else:
            self.fail('fixture registry did not complete')
        self.a, self.b = self.installs

    def test_external_edit_hints_and_wakes_only_selected_owner(self):
        source = self.b.project.write_note('docs/note.md', 'owner revision\n')
        import time
        rows = registry.routing_rows()
        targets = external._targets(str(self.a.project.root), time.monotonic() + 60)
        self.assertTrue(targets, 'registry=%r b_route=%r' % (rows, self.b.select()))
        with patch.object(runtime, 'request', return_value={'status': 'queued'}) as wake:
            result = external.notify([source], cwd=self.a.project.root, host='codex',
                                     origin=str(self.a.project.root))
        self.assertEqual(result, {'owners': 1, 'failed': 0, 'overflow': 0})
        wake.assert_called_once_with('laya-sync', str(self.b.project.root))
        decision = self.b.select()
        inbox = external._inbox_path(decision)
        row = external._read_box(inbox, decision)['edits'][0]
        body = source.read_bytes()
        blob = hashlib.sha1(b'blob ' + str(len(body)).encode() + b'\0' + body).hexdigest()
        self.assertEqual(row, {'path': 'docs/note.md', 'blob': blob, 'host': 'codex'})
        self.assertFalse(external._inbox_path(self.a.select()).exists())
        self.assertNotIn('owner revision', inbox.read_text())
