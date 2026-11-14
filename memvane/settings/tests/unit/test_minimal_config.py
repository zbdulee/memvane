"""Normalized config of minimal settings stays the same once the legacy branches are gone.

The two expected dicts are pinned from HEAD eeecc033 (`normalize_config` of a minimal
settings file and of an empty one, project folder `.memvane`).
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from memvane.settings import config

MINIMAL_SETTINGS = {"name": "Demo Proj", "skipPaths": ["drafts/"], "diagnostics": {"enabled": True}}

PINNED_MINIMAL = {'allowRoots': [],
 'collectionPaths': {'demo-proj': '.', 'demo-proj-wiki': '.memvane/wiki'},
 'collectionRoles': {'demo-proj': 'raw', 'demo-proj-wiki': 'wiki'},
 'collections': ['demo-proj', 'demo-proj-wiki'],
 'compile': {'batch': {'idleSeconds': 90, 'maxItems': 5},
             'budget': {'cardsPerSource': 10,
                        'dedupPairsPerCompile': 1,
                        'dedupPairsPerScan': 8,
                        'extractorPerRun': 10,
                        'verifyPerRun': 3},
             'defaultStatus': 'generated',
             'excludeStatusesFromRecall': ['discarded', 'contested'],
             'extractor': {'backends': {},
                           'builtins': ['claude', 'codex', 'hermes'],
                           'cooldownSeconds': 600,
                           'timeout': 120},
             'lowPriorityStatuses': ['generated', 'tentative'],
             'maxAutoPageLines': 120,
             'maxSourceChars': 12000,
             'mode': 'auto-wiki',
             'reasoningEffort': {'engines': {},
                                 'generation': 'low',
                                 'semanticDedup': 'medium',
                                 'verify': 'low'},
             'recallVerifiedOnly': True,
             'semanticDedup': {'candidateMinScore': 0.3,
                               'enabled': True,
                               'judge': {'cooldownSeconds': 600,
                                         'crossEngine': 'off',
                                         'enabled': True,
                                         'maxCharsPerPage': 6000,
                                         'timeout': 120},
                               'maxPairsPerScan': 10},
             'sourceScan': {'enabled': True, 'maxCardsPerScan': 300},
             'triggers': ['post_tool_source',
                          'post_sync_source',
                          'session_start_source',
                          'manual'],
             'verify': {'builtins': [],
                        'cooldownSeconds': 600,
                        'crossEngine': 'off',
                        'enabled': True,
                        'onFail': 'delete',
                        'onInconclusive': 'delete',
                        'timeout': 120}},
 'contextLearning': {'autoCycle': True, 'candidateTopK': 8, 'capture': True},
 'diagnostics': {'enabled': True},
 'events': ['sessionStart', 'userPromptSubmit', 'postToolUse'],
 'indexing': True,
 'injectSourcePathsPerCard': 3,
 'injectSummaryMaxChars': 600,
 'lexicalPatterns': [],
 'maintenance': {'orphanVectors': {'cooldownSeconds': 86400,
                                   'enabled': True,
                                   'minCount': 200,
                                   'minRatio': 0.2}},
 'minScore': 0.0,
 'name': 'Demo Proj',
 'prefixStyle': 'full',
 'queryTimeout': 5.0,
 'rawFallbackMinScore': 0.0,
 'recallStrategy': 'wikiOnly',
 'sessionStartSync': True,
 'settingsModel': 'minimal',
 'skipPaths': ['drafts/'],
 'staleQueueThreshold': 20,
 'topN': 3,
 'wikiPath': '.memvane/wiki'}

PINNED_EMPTY = {'allowRoots': [],
 'collectionPaths': {},
 'collectionRoles': {},
 'collections': [],
 'compile': {'batch': {'idleSeconds': 90, 'maxItems': 5},
             'budget': {'cardsPerSource': 10,
                        'dedupPairsPerCompile': 1,
                        'dedupPairsPerScan': 8,
                        'extractorPerRun': 10,
                        'verifyPerRun': 3},
             'defaultStatus': 'generated',
             'excludeStatusesFromRecall': ['discarded', 'contested'],
             'extractor': {'backends': {},
                           'builtins': [],
                           'cooldownSeconds': 600,
                           'timeout': 120},
             'lowPriorityStatuses': ['generated', 'tentative'],
             'maxAutoPageLines': 120,
             'maxSourceChars': 12000,
             'mode': 'off',
             'reasoningEffort': {'engines': {},
                                 'generation': 'low',
                                 'semanticDedup': 'medium',
                                 'verify': 'low'},
             'recallVerifiedOnly': True,
             'semanticDedup': {'candidateMinScore': 0.3,
                               'enabled': True,
                               'judge': {'cooldownSeconds': 600,
                                         'crossEngine': 'off',
                                         'enabled': True,
                                         'maxCharsPerPage': 6000,
                                         'timeout': 120},
                               'maxPairsPerScan': 10},
             'sourceScan': {'enabled': True, 'maxCardsPerScan': 300},
             'triggers': [],
             'verify': {'builtins': [],
                        'cooldownSeconds': 600,
                        'crossEngine': 'off',
                        'enabled': True,
                        'onFail': 'delete',
                        'onInconclusive': 'delete',
                        'timeout': 120}},
 'diagnostics': {'enabled': False},
 'events': ['sessionStart', 'userPromptSubmit', 'postToolUse'],
 'indexing': None,
 'injectSourcePathsPerCard': 3,
 'injectSummaryMaxChars': 600,
 'lexicalPatterns': [],
 'maintenance': {'orphanVectors': {'cooldownSeconds': 86400,
                                   'enabled': True,
                                   'minCount': 200,
                                   'minRatio': 0.2}},
 'minScore': 0.0,
 'name': '',
 'prefixStyle': 'full',
 'queryTimeout': 5.0,
 'rawFallbackMinScore': 0.0,
 'recallStrategy': 'hierarchical',
 'sessionStartSync': True,
 'skipPaths': [],
 'staleQueueThreshold': 20,
 'topN': 3,
 'wikiPath': '.memvane/wiki'}

# Values a hand-written legacy settings file could hold. None of them is a choice
# after decision 8: the compile block reads only what minimal expansion writes.
LEGACY_COMPILE = {
    "mode": "auto-wiki",
    "enabled": False,
    "autoWrite": False,
    "maxCardsPerSource": 4,
    "defaultStatus": "tentative",
    "extractor": {"backends": {"claude": ["python3", "fake.py"]}, "argv": ["x"]},
    "semanticDedup": {"judge": {"crossEngine": "require"}},
    "verify": {"onFail": "contested", "onInconclusive": "none", "crossEngine": "prefer"},
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


class MinimalConfigTest(unittest.TestCase):
    def test_minimal_settings_normalize_to_pinned_config(self):
        self.assertEqual(canonical(config.normalize_config(MINIMAL_SETTINGS, ".memvane")),
                         canonical(PINNED_MINIMAL))

    def test_empty_settings_normalize_to_pinned_config(self):
        self.assertEqual(canonical(config.normalize_config({}, ".memvane")),
                         canonical(PINNED_EMPTY))

    def test_settings_file_round_trip_matches_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve() / "proj"
            (project / ".memvane").mkdir(parents=True)
            (project / ".memvane" / "settings.json").write_text(
                json.dumps(MINIMAL_SETTINGS), encoding="utf-8")
            found = config.find_project_config(str(project))
        self.assertEqual(found["configFormat"], "auto-context-dir")
        self.assertEqual(canonical(found["config"]), canonical(PINNED_MINIMAL))

    def test_legacy_settings_file_is_refused(self):
        legacy = {**MINIMAL_SETTINGS, "compile": LEGACY_COMPILE}
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp).resolve() / "proj"
            (project / ".memvane").mkdir(parents=True)
            (project / ".memvane" / "settings.json").write_text(
                json.dumps(legacy), encoding="utf-8")
            found = config.find_project_config(str(project))
            raw = config.load_project_config_raw(str(project))
        expected = {**PINNED_EMPTY, "collections": []}
        self.assertEqual(canonical(found["config"]), canonical(expected))
        self.assertEqual(raw, {})

    def test_legacy_compile_values_have_no_effect(self):
        legacy = config.compile_config(LEGACY_COMPILE)
        plain = config.compile_config({"mode": "auto-wiki"})
        self.assertEqual(canonical(legacy), canonical(plain))
        self.assertEqual(legacy["mode"], "auto-wiki")
        self.assertEqual(legacy["extractor"]["backends"], {})
        self.assertEqual(legacy["budget"]["cardsPerSource"], 10)
        self.assertEqual((legacy["verify"]["onFail"], legacy["verify"]["onInconclusive"],
                          legacy["verify"]["crossEngine"],
                          legacy["semanticDedup"]["judge"]["crossEngine"],
                          legacy["defaultStatus"]),
                         ("delete", "delete", "off", "off", "generated"))

    def test_deprecated_key_handling_is_gone(self):
        names = ("deprecated_keys", "deprecated_key_notice", "invalid_compile_flags",
                 "DEPRECATED_RELOCATED_KEYS", "DEPRECATED_FOLDED_KEYS",
                 "DEPRECATED_REMOVED_KEYS", "VERIFY_ON_FAIL", "extractor_backends")
        self.assertEqual([name for name in names if hasattr(config, name)], [])


if __name__ == "__main__":
    unittest.main()
