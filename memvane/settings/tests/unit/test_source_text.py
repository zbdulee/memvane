import unittest
from memvane.settings import api


class SourceTextTests(unittest.TestCase):
    def test_extension_matching_is_case_insensitive_and_denies_dependency_paths(self):
        names = ('a.md', 'A.TXT', 'b.Rst', 'c.adoc', 'd.mdx', 'e.markdown',
                 'f.py', 'g.json', 'md')
        self.assertEqual([api.is_source_text(name) for name in names],
                         [True, True, True, True, True, True, False, False, False])
        paths = ('docs/node_modules/x.md', 'build/a.md', 'app/Library/PackageCache/x.md',
                 'app/Temp/x.md', 'app/obj/x.md', 'docs/a.md', 'docs/builder/a.md',
                 'docs/library/a.md', '.memvane/wiki/card.md', '.git/config')
        self.assertEqual([api.is_denied_source_path(path) for path in paths],
                         [True, True, True, True, True, False, False, False, True, True])
