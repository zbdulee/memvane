"""Registry routing holds edits when nested checks or bounded enumeration expire."""
import os
from types import SimpleNamespace
from unittest.mock import patch

from memvane.registry import api as registry
from memvane.runtime import api as runtime
from memvane.wiki.intake import external_edits as external
from tests.support.case import FixtureCase
from tests.support.project import make_project


class ExternalRegistryGuards(FixtureCase):
    def setUp(self):
        super().setUp()
        self.project = make_project(self.home, git=False)
        self.root = self.project.root
        self.source = self.project.write_note('nested/note.md', 'synthetic source\n')

    def test_nested_check_at_the_deadline_holds_without_looking(self):
        with patch.object(external, 'time', new=SimpleNamespace(monotonic=lambda: 10.0)), \
             patch.object(os, 'stat', wraps=os.stat) as observed:
            self.assertTrue(external._nested_project(self.source, str(self.root), 10.0))
        self.assertEqual(observed.call_count, 0)
        with patch.object(external, 'time', new=SimpleNamespace(monotonic=lambda: 10.0)):
            self.assertFalse(external._nested_project(self.source, str(self.root), 11.0))

    def test_deadline_passing_during_last_nested_stat_holds_the_edit(self):
        now = [0.0]
        real_stat = os.stat
        last = self.source.parent / '.memvane/settings.json'
        touched = []
        def stat(path, *args, **kwargs):
            if str(path) == str(last):
                touched.append(True)
                now[0] = 100.0
            return real_stat(path, *args, **kwargs)
        target = {'projectRoot': str(self.root), 'rawRoot': str(self.root), 'registered': True,
                  'decision': {'projectRoot': str(self.root), 'recallSettings': {'skipPaths': []}}}
        with patch.object(external, 'time', new=SimpleNamespace(monotonic=lambda: now[0])), \
             patch.object(external, '_targets', return_value=[target]), \
             patch.object(os, 'stat', side_effect=stat), \
             patch.object(runtime, 'request') as wake:
            result = external.notify([self.source], cwd=self.home, host='codex', origin=None)
        self.assertEqual(touched, [True])
        self.assertEqual(result, {'owners': 0, 'failed': 0, 'overflow': 0})
        wake.assert_not_called()

    def test_registry_rows_over_install_cap_refuse_without_routing(self):
        rows = [{'installId': char * 32, 'stateRoot': str(self.home / (char * 32)),
                 'projectRoot': str(self.root)} for char in ('a', 'b')]
        with patch.object(registry, 'routing_rows', return_value={'complete': True, 'rows': rows}), \
             patch.object(registry, 'is_home_install', return_value=False), \
             patch.object(external, 'MAX_INSTALLS', 1), \
             patch.object(runtime, 'request') as wake:
            result = external.notify([self.source], cwd=self.home, host='codex', origin=None)
        self.assertEqual(result, {'owners': 0, 'failed': 1, 'overflow': 0})
        wake.assert_not_called()
