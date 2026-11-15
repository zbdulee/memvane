"""The 1.0 minimal settings select portable built-in extractor entries."""
import unittest

from memvane.settings import api as settings
from memvane.wiki.engines import api as engines


class CompileDefaults(unittest.TestCase):
    def test_minimal_settings_resolve_all_builtins_through_entry(self):
        config = settings.expand_minimal_settings({'name': 'fixture', 'skipPaths': []})
        compile_cfg = config['compile']
        self.assertEqual(compile_cfg['mode'], 'auto-wiki')
        self.assertIn('post_tool_source', compile_cfg['triggers'])
        self.assertEqual(compile_cfg['extractor']['builtins'], ['claude', 'codex', 'hermes'])
        for engine in ('claude', 'codex', 'hermes'):
            with self.subTest(engine=engine):
                argv = engines.resolve_extractor_argv(compile_cfg, engine)
                self.assertTrue(any(str(arg).endswith('entry/run.py') for arg in argv))
                self.assertIn('extractor-' + engine, argv)
