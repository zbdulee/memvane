"""Verifier stamps bind source spans to the verdict in one write."""
from memvane.wiki.card import api as card_api
from memvane.wiki.card import source_spans, yaml_scalars
from memvane.wiki.card.tests.unit.test_source_spans import DATA, SHA, PATH, claim, evidence
from memvane.wiki.verify import api as verify_api
from tests.support.case import FixtureCase


class StampChecks(FixtureCase):
    def setUp(self):
        super().setUp()
        source = self.home / 'docs' / 'source.md'
        source.parent.mkdir()
        source.write_bytes(DATA)
        stat = source.stat()
        revision = {'kind': 'file', 'path': PATH, 'collection': 'raw', 'sha256': SHA,
                    'size': stat.st_size, 'mtimeNs': stat.st_mtime_ns}
        [emitted] = yaml_scalars.dump_source_revisions([revision])
        self.card = self.home / 'card.md'
        self.card.write_text(
            '---\ntitle: "Card"\nstatus: generated\ncreatedBy: memvane\n\n'
            'sources:\n  - {kind: "file", path: "docs/source.md"}\n'
            f'sourceRevisions:\n  - {emitted}\n---\n\n## Summary\n포트는 8080이다.\n',
            encoding='utf-8')
        self.spans = source_spans.derive_spans(
            [claim('포트는 8080'), claim('of fact\ncontinues')], evidence())
        self.body_hash = yaml_scalars.card_body_hash(self.card.read_text(encoding='utf-8'))

    def stamp(self, status='verified', spans=None):
        return verify_api.stamp_verification(self.card, status, 'codex', 'cross-engine',
                                             self.body_hash, source_spans=spans)

    def frontmatter(self):
        text = self.card.read_text(encoding='utf-8')
        return text, yaml_scalars.FRONTMATTER_RE.match(text).group(1)

    def test_verified_stamp_writes_status_and_spans_together_without_quotes(self):
        self.assertTrue(self.stamp(spans=self.spans))
        text, block = self.frontmatter()
        self.assertRegex(block, r'(?m)^status: verified$')
        self.assertEqual(source_spans.read_card_spans(block), (source_spans.PRESENT, self.spans))
        self.assertNotIn('포트는 8080', block)
        self.assertNotIn('continues', block)
        self.assertEqual(yaml_scalars.card_body_hash(text), self.body_hash)

    def test_contested_or_spanless_restamp_and_status_reset_clear_spans(self):
        for action in ('contested', 'verified_without_spans', 'status_reset'):
            with self.subTest(action=action):
                self.assertTrue(self.stamp(spans=self.spans))
                if action == 'contested':
                    self.assertTrue(self.stamp('contested'))
                elif action == 'verified_without_spans':
                    self.assertTrue(self.stamp())
                else:
                    text = self.card.read_text(encoding='utf-8')
                    rendered = card_api.render_frontmatter_fields(text, {'status': 'generated'})
                    self.assertIsNotNone(rendered)
                    self.card.write_text(rendered, encoding='utf-8')
                _text, block = self.frontmatter()
                self.assertNotIn('sourceSpans', block)
                self.assertEqual(source_spans.read_card_spans(block), (source_spans.ABSENT, []))

    def test_unbound_or_non_verified_spans_write_nothing(self):
        before = self.card.read_bytes()
        decoy = [{**self.spans[0], 'path': 'docs/other.md'}]
        stale = [{**self.spans[0], 'sourceSha256': 'c' * 64}]
        for status, spans in (('verified', decoy), ('verified', stale), ('contested', self.spans)):
            with self.subTest(status=status, path=spans[0]['path']):
                self.assertFalse(self.stamp(status, spans))
                self.assertEqual(self.card.read_bytes(), before)

    def test_tampered_duplicate_headers_are_replaced_by_one_block(self):
        text = self.card.read_text(encoding='utf-8')
        self.card.write_text(text.replace('status: generated\n',
            'status: generated\nsourceSpans:\n  - {junk: 1}\n"sourceSpans":\n  - x\n'),
            encoding='utf-8')
        self.body_hash = yaml_scalars.card_body_hash(self.card.read_text(encoding='utf-8'))
        self.assertTrue(self.stamp(spans=self.spans))
        _text, block = self.frontmatter()
        self.assertEqual(block.count('sourceSpans'), 1)
        self.assertNotIn('junk', block)
        self.assertEqual(source_spans.read_card_spans(block), (source_spans.PRESENT, self.spans))
