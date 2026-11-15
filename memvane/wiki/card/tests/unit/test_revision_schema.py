import unittest
from memvane.wiki.card import yaml_scalars


class RevisionSchemaTests(unittest.TestCase):
    def setUp(self):
        self.record = {'kind': 'file', 'path': 'docs/source.md', 'collection': 'raw',
                       'sha256': 'a' * 64, 'size': 11, 'mtimeNs': 456}

    def test_closed_schema_round_trip_and_unknown_key_rejection(self):
        rows = yaml_scalars.dump_source_revisions([self.record])
        self.assertEqual(len(rows), 1)
        self.assertEqual(yaml_scalars.load_source_revisions(rows), [self.record])
        self.assertEqual(yaml_scalars.load_source_revisions([
            rows[0].replace('mtimeNs: 456', 'mtimeNs: 456, extra: no')]), [])

    def test_quoted_numbers_and_empty_entries_reject_the_whole_record(self):
        row = yaml_scalars.dump_source_revisions([self.record])[0]
        for bad in (row.replace('size: 11', 'size: "11"'),
                    row.replace(', sha256:', ',, sha256:')):
            with self.subTest(row=bad):
                self.assertEqual(yaml_scalars.load_source_revisions([bad]), [])
        self.assertEqual(yaml_scalars.dump_source_revisions([
            self.record, {**self.record, 'size': '11'}]), [])
