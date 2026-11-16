"""Freshness compares only sources admitted by the current skip policy."""
import unittest

from tests.support.case import FixtureCase
from tests.support.install_builder import build_install
from tests.support.project import make_project


class SkipFreshness(FixtureCase):
    def ready(self, *, name='fixture', ident='4' * 32, card=False):
        project = make_project(self.home, name=name, dirname=name, skip_paths=['output'])
        note = project.write_note('notes/allowed.md', 'Original allowed note.\n')
        project.write_note('output/generated.md', 'Generated and excluded.\n')
        if card:
            project.write_card('concepts/card.md', title='Allowed note',
                               body='Original allowed note.\n', sources=[note])
        project.commit()
        install = build_install(self, project, upto='ready', install_id=ident)
        return project, install

    def head(self, install):
        with install.db() as db:
            return db.execute('SELECT g.gen_id,h.epoch FROM s_heads h JOIN s_generations g '
                              'ON g.view_id=h.view_id AND g.ordinal=h.ordinal').fetchone()

    def test_empty_wiki_keeps_generation_and_finalizes_with_excluded_sources(self):
        project, install = self.ready()
        before = self.head(install)
        project.write_note('output/generated.md', 'Changed but still excluded.\n')
        idle = install.run_sync()
        self.assertEqual(idle['state'], 'unchanged', idle)
        self.assertEqual(self.head(install), before)
        self.assertEqual(install.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(install.activate()['status'], 'selected')

    def test_existing_selected_install_does_not_republish_every_pass(self):
        _project, install = self.ready(card=True)
        self.assertEqual(install.finalize()['status'], 'initial_build_finalized')
        self.assertEqual(install.activate()['status'], 'selected')
        install.settle()
        before = self.head(install)
        for _ in range(2):
            result = install.run_sync()
            self.assertEqual(result['state'], 'unchanged', result)
            self.assertEqual(self.head(install), before)

    def test_allowed_changes_and_skip_policy_changes_remain_stale(self):
        for index, change in enumerate(('modify', 'add', 'delete', 'unskip', 'new-skip'), 1):
            with self.subTest(change=change):
                project, install = self.ready(name='case%d' % index, ident=str(index) * 32)
                note = project.root / 'notes/allowed.md'
                if change == 'modify':
                    note.write_text('Changed allowed note.\n', encoding='utf-8')
                elif change == 'add':
                    project.write_note('notes/added.md', 'New allowed note.\n')
                elif change == 'delete':
                    note.unlink()
                elif change == 'unskip':
                    project.write_settings(skip_paths=[])
                else:
                    project.write_settings(skip_paths=['output', 'not-present'])
                result = install.finalize()
                self.assertEqual((result['status'], result.get('reason')),
                                 ('initial_build_pending', 'initial_build_out_of_date'), result)

    def test_activate_refuses_changes_after_finalize(self):
        for index, change in enumerate(('allowed', 'skip-policy'), 1):
            with self.subTest(change=change):
                project, install = self.ready(name='activate%d' % index, ident=str(index + 5) * 32)
                self.assertEqual(install.finalize()['status'], 'initial_build_finalized')
                if change == 'allowed':
                    project.write_note('notes/allowed.md', 'Changed after finalize.\n')
                else:
                    project.write_settings(skip_paths=['output', 'not-present'])
                with self.assertRaisesRegex(Exception, '^initial_build_out_of_date$'):
                    install.activate()


if __name__ == '__main__':
    unittest.main()
