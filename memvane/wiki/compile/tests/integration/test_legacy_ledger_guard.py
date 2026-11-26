"""Surviving legacy creation and deletion evidence constrains only related writes."""
import json

from memvane.wiki.compile.tests.integration.test_writer_lifecycle import WriterFixture
from memvane.wiki.workspace import api as workspace


class LegacyLedgerGuard(WriterFixture):
    def test_mixed_manifest_allows_only_unrelated_append_and_preserves_the_locked_source(self):
        first = self.compile()
        target = self.project.root / first['targetPath']
        target.unlink()
        folder = self.project.root / '.memvane/compile'
        manifest = folder / 'generated-manifest.jsonl'
        row = json.loads(manifest.read_bytes())
        row['sources'][0]['path'] = 'docs/old\u2029source.md'
        row['targetResolution'] = 'explicit'
        # A normal implicit row bridges the otherwise unrelated explicit target.
        bridge = dict(row, targetPath='wiki/bridge.md', targetResolution='slug', sources=[],
                      canonicalKey='bridge', sourceHash=row['sourceHash'])
        linked = dict(bridge, targetPath='wiki/linked.md', sourceHash='previous', targetResolution='explicit')
        original = b''.join((json.dumps(r, ensure_ascii=False) + '\r\n').encode() for r in (row, bridge, linked))
        manifest.write_bytes(original)
        self.assertEqual(self.compile(targetPath=first['targetPath'])['action'], 'deferred')
        linked_rel = (self.project.wiki / 'concepts/linked.md').relative_to(self.project.root).as_posix()
        self.assertEqual(self.compile(targetPath=linked_rel, canonicalKey='bridge')['action'], 'deferred')
        self.project.write_note('docs/new.md', 'Independent source.')
        normal = self.compile(title='A New Source', canonicalKey='independent', summary='Independent.',
                              sources=[{'kind': 'file', 'path': 'docs/new.md'}])
        self.assertEqual(normal['action'], 'created', normal)
        after = manifest.read_bytes()
        self.assertTrue(after.startswith(original))
        self.assertEqual(json.loads(after[len(original):])['targetPath'], normal['targetPath'])
        workspace.compact_manifest(manifest, max_bytes=0)
        self.assertEqual(manifest.read_bytes(), after)
        self.assertFalse(target.exists())

    def test_unproven_public_generations_stay_byte_identical(self):
        first = self.compile()
        target = self.project.root / first['targetPath']
        target.unlink()
        folder = self.project.root / '.memvane/compile'
        manifest = folder / 'generated-manifest.jsonl'
        row = json.loads(manifest.read_bytes())
        for sep in ('\x85', '\u2028', '\u2029'):
            row['sources'][0]['path'] = 'docs/a' + sep + 'b.md'
            for escaped in (False, True):
                original = (json.dumps(row, ensure_ascii=escaped) + '\r\n').encode() * 2
                manifest.write_bytes(original)
                for _ in range(2):
                    workspace.compact_manifest(manifest, max_bytes=0)
                    self.assertEqual(self.compile(targetPath=first['targetPath'])['action'], 'deferred')
                self.assertEqual(manifest.read_bytes(), original)
                self.assertFalse(target.exists())
        for raw in (b'{"targetPath":"x","targetPath":"y"}\n', b'{"action":"future","targetPath":"x"}\n',
                    b'{"action":"created","targetPath":"x"}', b'{"action":"created","targetPath":"x","v":NaN}\n',
                    b'{"targetPath":"\xff"}\n'):
            manifest.write_bytes(raw)
            self.assertEqual(self.compile()['action'], 'deferred')
            self.assertEqual(manifest.read_bytes(), raw)
