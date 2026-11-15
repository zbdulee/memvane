from tests.support.case import FixtureCase
from tests.support.project import make_project
from memvane.wiki.card import api


class CardTrustTests(FixtureCase):
    def test_verified_owned_card_requires_source_provenance(self):
        project = make_project(self.home, git=False)
        source = project.write_note('docs/source.md', 'documented fact')
        decision = {'projectRoot': str(project.root),
                    'collectionRoots': {project.raw_name: str(project.root)},
                    'collections': [{'name': project.raw_name, 'role': 'raw'}]}

        def check(body):
            reasons = {}
            accepted = api.verified_wiki_row({'path': 'wiki/card.md', 'body': body},
                                             decision, reasons=reasons)
            return accepted, reasons

        good = project.card_text(title='Card', body='fact', sources=[source])
        self.assertTrue(check(good)[0])
        for text, reason in (
            (project.card_text(title='Card', body='fact', sources=[source], status='draft'),
             'status_not_verified'),
            (project.card_text(title='Card', body='fact', sources=[source], created_by='foreign'),
             'creator_foreign'),
            (project.card_text(title='Card', body='fact'), 'source_revisions_missing'),
            (good.replace('fact', 'modified'), 'body_digest_mismatch'),
        ):
            with self.subTest(reason=reason):
                accepted, reasons = check(text)
                self.assertFalse(accepted)
                self.assertEqual(reasons, {reason: 1})
        source.write_text('changed', encoding='utf-8')
        self.assertEqual(check(good), (False, {'source_changed': 1}))
