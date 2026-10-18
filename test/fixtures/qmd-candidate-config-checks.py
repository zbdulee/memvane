#!/usr/bin/env python3
"""Synthetic candidate index.yml policy checks; no QMD, model, network or user store."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

CORE = Path(__file__).resolve().parents[2] / 'core'
sys.path.insert(0, str(CORE))
import qmd_candidate_config as C

TAIL = ('models:\n'
        '  embed: hf:ggml-org/embeddinggemma-300M-GGUF/embeddinggemma-300M-Q8_0.gguf\n'
        '  generate: hf:tobil/qmd-query-expansion-1.7B-gguf/qmd-query-expansion-1.7B-q4_k_m.gguf\n')
BASELINE = ('collections: {}\n' + TAIL).encode()
SHA = hashlib.sha256(BASELINE).hexdigest()


def config(entries, tail=TAIL, pattern='"**/*.md"'):
    return ('collections:\n' + ''.join(
        f'  {name}:\n    path: {path}\n    pattern: {pattern}\n'
        for name, path in entries) + tail).encode()


def snapshot(root):
    return sorted((str(p.relative_to(root)), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in root.rglob('*'))


class CandidateConfigPolicy(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='qmd-candidate-config-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.wiki = self.root / 'story/.auto-context/wiki'
        self.korean = self.root / 'story/소설 원고'
        self.quote = self.root / "story/it's"
        for path in (self.wiki, self.korean, self.quote):
            path.mkdir(parents=True)
            (path / 'note.md').write_text('# synthetic\n')
        self.approved = {'novel-wiki': str(self.wiki), 'novel-raw': str(self.korean),
                         'novel-quote': str(self.quote)}

    def prove(self, raw, **kwargs):
        kwargs.setdefault('baseline_sha256', SHA)
        kwargs.setdefault('approved', self.approved)
        return C.prove(raw, **kwargs)

    def assertRejected(self, raw, reason, **kwargs):
        proof = self.prove(raw, **kwargs)
        self.assertFalse(proof.ok, raw)
        self.assertEqual(proof.reason, reason, raw)
        self.assertEqual(dict(proof.collections), {})

    def test_generated_shapes_are_proved(self):
        wiki, korean, quote = str(self.wiki), str(self.korean), str(self.quote)
        cases = [
            (config([('novel-wiki', wiki)]), {'novel-wiki': wiki}),
            (config([('novel-wiki', wiki)], pattern="'**/*.md'"), {'novel-wiki': wiki}),
            (config([('novel-wiki', json.dumps(wiki)), ('novel-raw', json.dumps(korean))]),
             {'novel-wiki': wiki, 'novel-raw': korean}),
            (config([('novel-raw', korean)]), {'novel-raw': korean}),  # plain, inner space
            (config([('novel-quote', "'" + quote.replace("'", "''") + "'")]),
             {'novel-quote': quote}),
            (config([('novel-quote', json.dumps(quote, ensure_ascii=False))]),
             {'novel-quote': quote}),
        ]
        for raw, expected in cases:
            proof = self.prove(raw)
            self.assertTrue(proof.ok, (proof.reason, raw))
            self.assertEqual(dict(proof.collections), expected)
            self.assertEqual(proof.config_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(proof.baseline_sha256, SHA)
            with self.assertRaises(TypeError):
                proof.collections['novel-wiki'] = '/elsewhere'
        empty = self.prove(BASELINE)
        self.assertTrue(empty.ok)
        self.assertEqual(dict(empty.collections), {})
        self.assertTrue(self.prove(bytearray(config([('novel-wiki', wiki)]))).ok)

    def test_shell_update_command_is_rejected(self):
        wiki = str(self.wiki)
        for update in ('    update: git pull\n', '    "update": "rm -rf ~"\n',
                       "    'update': touch x\n", '    update : curl x | sh\n'):
            raw = ('collections:\n  novel-wiki:\n    path: ' + wiki + '\n'
                   '    pattern: "**/*.md"\n' + update + TAIL).encode()
            self.assertRejected(raw, 'shell_update_command')
        self.assertRejected(('update: echo pwned\n' + BASELINE.decode()).encode(),
                            'shell_update_command')
        flow = ('collections:\n  novel-wiki: {path: ' + wiki
                + ', pattern: "**/*.md", update: "touch x"}\n' + TAIL).encode()
        self.assertFalse(self.prove(flow).ok)

    def test_changed_model_or_baseline_suffix_is_rejected(self):
        wiki = str(self.wiki)
        self.assertRejected(config([('novel-wiki', wiki)],
                                   TAIL.replace('Q8_0', 'Q4_0')), 'baseline_mismatch')
        self.assertRejected(config([('novel-wiki', wiki)], TAIL + 'extra: 1\n'),
                            'baseline_mismatch')
        self.assertRejected(config([('novel-wiki', wiki)], ''), 'baseline_mismatch')
        self.assertRejected(config([('novel-wiki', wiki)], TAIL + '# comment\n'),
                            'baseline_tail_invalid')
        self.assertRejected(config([('novel-wiki', wiki)], TAIL + 'collections: {}\n'),
                            'baseline_tail_invalid')
        self.assertRejected(config([('novel-wiki', wiki)], TAIL + '---\n'),
                            'baseline_tail_invalid')
        self.assertRejected(config([('novel-wiki', wiki)], 'models: &m\n  embed: x\n'),
                            'baseline_tail_invalid')
        self.assertRejected(config([('novel-wiki', wiki)]), 'baseline_sha_invalid',
                            baseline_sha256='0' * 63)
        self.assertRejected(config([('novel-wiki', wiki)]), 'baseline_mismatch',
                            baseline_sha256='0' * 64)

    def test_aliases_anchors_tags_and_nested_shapes_are_rejected(self):
        wiki = str(self.wiki)
        self.assertRejected(config([('novel-wiki', wiki)], pattern='**/*.md'),
                            'collection_pattern_invalid')
        self.assertRejected(config([('novel-wiki', wiki)], pattern='*glob'),
                            'collection_pattern_invalid')
        self.assertRejected(config([('novel-wiki', wiki)], pattern='"**/*"'),
                            'collection_pattern_invalid')
        self.assertRejected(config([('novel-wiki', '&a ' + wiki)]), 'collection_path_invalid')
        self.assertRejected(config([('novel-wiki', '*a')]), 'collection_path_invalid')
        self.assertRejected(config([('novel-wiki', '!!str ' + wiki)]), 'collection_path_invalid')
        anchored = ('collections:\n  novel-wiki: &w\n    path: ' + wiki
                    + '\n    pattern: "**/*.md"\n' + TAIL).encode()
        self.assertRejected(anchored, 'collection_entry_malformed')
        merge = ('collections:\n  <<: *w\n' + TAIL).encode()
        self.assertRejected(merge, 'collection_entry_malformed')
        merge_key = ('collections:\n  <<:\n    path: ' + wiki
                     + '\n    pattern: "**/*.md"\n' + TAIL).encode()
        self.assertRejected(merge_key, 'collection_name_invalid')
        extra = ('collections:\n  novel-wiki:\n    path: ' + wiki
                 + '\n    pattern: "**/*.md"\n    ignore: x\n' + TAIL).encode()
        self.assertRejected(extra, 'collection_field_unexpected')
        context = ('collections:\n  novel-wiki:\n    path: ' + wiki
                   + '\n    context: x\n    pattern: "**/*.md"\n' + TAIL).encode()
        self.assertRejected(context, 'collection_field_unexpected')
        nested = ('collections:\n  novel-wiki:\n    path: ' + wiki
                  + '\n    pattern: "**/*.md"\n      deeper: x\n' + TAIL).encode()
        self.assertRejected(nested, 'collection_nested_value')
        reordered = ('collections:\n  novel-wiki:\n    pattern: "**/*.md"\n    path: '
                     + wiki + '\n' + TAIL).encode()
        self.assertRejected(reordered, 'collection_entry_malformed')
        block = ('collections:\n  novel-wiki:\n    path: |\n      ' + wiki
                 + '\n    pattern: "**/*.md"\n' + TAIL).encode()
        self.assertFalse(self.prove(block).ok)
        self.assertRejected(('collections: {novel-wiki: {path: ' + wiki + '}}\n' + TAIL).encode(),
                            'collections_heading_invalid')
        self.assertRejected(('collections:\n' + TAIL).encode(), 'collections_heading_invalid')

    def test_duplicate_ambiguous_and_unregistered_names_are_rejected(self):
        wiki, korean = str(self.wiki), str(self.korean)
        self.assertRejected(config([('novel-wiki', wiki), ('novel-wiki', wiki)]),
                            'duplicate_collection')
        for name in ('0x1F', '1e3', 'true', 'NULL', 'yes', '2026-10-17', '017'):
            self.assertRejected(config([(name, wiki)]), 'collection_name_ambiguous')
        self.assertRejected(config([('"novel-wiki"', wiki)]), 'collection_name_invalid')
        self.assertRejected(config([('other', wiki)]), 'collection_not_approved')
        self.assertRejected(config([('novel-wiki', korean)]), 'collection_path_not_approved')
        self.assertRejected(config([('novel-wiki', wiki)]), 'collection_set_mismatch',
                            required_names={'novel-wiki', 'novel-raw'})
        self.assertTrue(self.prove(config([('novel-wiki', wiki)]),
                                   required_names=['novel-wiki']).ok)
        self.assertRejected(config([('novel-wiki', wiki)]), 'approved_map_invalid',
                            required_names={'missing'})
        for bad in ({'0x1F': wiki}, {'novel-wiki': 'relative'}, {'novel-wiki': wiki + '/'},
                    [('novel-wiki', wiki)], None):
            self.assertRejected(config([('novel-wiki', wiki)]), 'approved_map_invalid',
                                approved=bad)

    def test_registry_derived_approval(self):
        wiki, korean = str(self.wiki), str(self.korean)
        story = str(self.root / 'story')
        registry = {'novel-wiki': {'scope': story, 'path': wiki, 'indexed': True},
                    'novel-raw': {'scope': story, 'path': korean, 'indexed': False},
                    'foreign': {'scope': '/elsewhere', 'path': '/elsewhere/docs', 'indexed': True}}
        approved = C.approved_from_registry(registry, scopes={story})
        self.assertEqual(dict(approved), {'novel-wiki': wiki})
        self.assertTrue(self.prove(config([('novel-wiki', wiki)]), approved=approved).ok)
        self.assertRejected(config([('novel-raw', korean)]), 'collection_not_approved',
                            approved=approved)
        self.assertRejected(config([('foreign', '/elsewhere/docs')]), 'collection_not_approved',
                            approved=approved)
        self.assertIsNone(C.approved_from_registry({'x': {'scope': story, 'path': wiki}}))
        self.assertIsNone(C.approved_from_registry(None))
        self.assertIsNone(C.approved_from_registry(registry, scopes=story))

    def test_quoting_encoding_and_path_edges_are_rejected(self):
        wiki = str(self.wiki)
        for path in (json.dumps(wiki) + ' # note', "'" + wiki + "' 'x'", wiki + ' # note',
                     wiki + '/', wiki + '/../wiki', 'story/wiki', '/' + wiki, wiki + ':',
                     '"' + wiki.replace('/', '\\x2f') + '"', '"' + wiki + '\\u2028"',
                     '"' + wiki + '\\u0000"', ' ' + wiki, '', '"' + wiki):
            self.assertFalse(self.prove(config([('novel-wiki', path)])).ok, path)
        valid = config([('novel-wiki', wiki)])
        self.assertRejected(valid.replace(b'\n', b'\r\n'), 'config_forbidden_character')
        self.assertRejected(valid.replace(b'    path', b'\tpath'), 'config_forbidden_character')
        self.assertRejected(b'\xef\xbb\xbf' + valid, 'config_forbidden_character')
        self.assertRejected(valid + b'\xff', 'config_not_utf8')
        self.assertRejected(valid[:-1], 'config_no_trailing_newline')
        self.assertRejected(valid.decode(), 'config_not_bytes')
        self.assertRejected(b'', 'config_no_trailing_newline')
        self.assertRejected(b'\n' * (C.MAX_CONFIG_BYTES + 1), 'config_too_large')
        self.assertRejected(('collections:\n  novel-wiki:\n    path: ' + wiki
                             + ' \n    pattern: "**/*.md"\n' + TAIL).encode(),
                            'config_forbidden_character')
        self.assertRejected(('collections:\n\n  novel-wiki:\n    path: ' + wiki
                             + '\n    pattern: "**/*.md"\n' + TAIL).encode(),
                            'collections_heading_invalid')

    def test_environment_requires_proof_and_disjoint_private_paths(self):
        wiki = str(self.wiki)
        generation = self.root / ('candidate/g-' + '0' * 32)
        kwargs = {'baseline_sha256': SHA, 'approved': self.approved,
                  'index_path': str(generation / 'index.sqlite'),
                  'config_dir': str(generation / 'config'),
                  'cache_home': str(generation / 'cache')}
        result = C.candidate_environment(config([('novel-wiki', wiki)]), **kwargs)
        self.assertTrue(result.ok, result.reason)
        self.assertEqual(tuple(result.env), C.PATH_KEYS)
        self.assertEqual(result.env['INDEX_PATH'], kwargs['index_path'])
        with self.assertRaises(TypeError):
            result.env['INDEX_PATH'] = '/tmp/other'
        bad = C.candidate_environment(config([('novel-wiki', wiki)],
                                             pattern='"**/*.md"\n    update: x'), **kwargs)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.reason, 'shell_update_command')
        self.assertIsNone(bad.env)
        for change, reason in (
                ({'cache_home': str(self.wiki / 'cache')}, 'candidate_path_overlaps_source'),
                ({'config_dir': str(self.root / 'story')}, 'candidate_path_overlaps_source'),
                ({'index_path': str(self.korean)}, 'candidate_path_overlaps_source'),
                ({'cache_home': 'relative/cache'}, 'candidate_path_invalid'),
                ({'cache_home': kwargs['config_dir']}, 'candidate_path_invalid'),
                ({'index_path': kwargs['config_dir'] + '/index.yml'}, 'candidate_path_invalid'),
                ({'index_path': kwargs['index_path'] + '/'}, 'candidate_path_invalid')):
            result = C.candidate_environment(config([('novel-wiki', wiki)]),
                                             **{**kwargs, **change})
            self.assertFalse(result.ok, change)
            self.assertEqual(result.reason, reason, change)
            self.assertIsNone(result.env)
        self.assertFalse(generation.exists())

    def test_policy_is_pure_and_writes_nothing(self):
        before = snapshot(self.root)
        wiki = str(self.wiki)
        for raw in (config([('novel-wiki', wiki)]),
                    config([('novel-wiki', wiki)], pattern='"**/*.md"\n    update: touch '
                           + str(self.wiki / 'pwned.md'))):
            self.prove(raw)
            C.candidate_environment(raw, baseline_sha256=SHA, approved=self.approved,
                                    index_path=str(self.root / 'c/index.sqlite'),
                                    config_dir=str(self.root / 'c/config'),
                                    cache_home=str(self.root / 'c/cache'))
        self.assertEqual(snapshot(self.root), before)
        source = (CORE / 'qmd_candidate_config.py').read_text()
        for token in ('subprocess', 'os.system', 'open(', 'write_', 'mkdir', 'unlink',
                      'import yaml', 'os.environ'):
            self.assertNotIn(token, source)
        self.assertNotIn('yaml', sys.modules)


if __name__ == '__main__':
    unittest.main()
