"""Mixed host edit retries card admission even when index ACK ran first."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'core'))

import edit_context
import posttool
from context_learning import source_card_provenance, synthetic_experiment, synthetic_scope


with tempfile.TemporaryDirectory(prefix='qmd-posttool-mixed-', dir='/tmp') as tmp:
    project = Path(tmp) / 'novel'
    story = project / 'story'
    story.mkdir(parents=True)
    shared = project / 'shared'
    shared.mkdir()
    raw = '01_Settings/qmd-prospective/rule.md'
    card = '.auto-context/wiki/qmd-prospective/rule.md'
    payload = {'cwd': str(story), 'hook_event_name': 'PostToolUse',
               'tool_name': 'apply_patch', 'tool_input': {'patch':
                   '*** Begin Patch\n*** Add File: ' + raw + '\n+rule\n'
                   '*** Add File: ' + card + '\n+card\n*** End Patch\n'}}
    event = {'changes': [{'path': raw}, {'path': card}]}
    found = {'projectRoot': str(project), 'config': {
        'collections': ['raw', 'wiki'], 'indexing': True,
        'collectionPaths': {'raw': 'story/01_Settings',
                            'wiki': 'story/.auto-context/wiki'}}}
    retried = []
    def reconcile(_root, _project, *, card_filter):
        retried.append(card_filter)
        return {'registered': ['registered-after-index-ack'],
                'deferred': [], 'eligible': 1, 'skipped': 0}
    output = io.StringIO()
    with patch.object(posttool.qmd_config, 'find_project_config', return_value=found), \
            patch.object(posttool.qmd_config, 'event_enabled', return_value=True), \
            patch.object(synthetic_scope, 'invocation_in_generated_story', return_value=True), \
            patch.object(synthetic_scope, 'NOVEL_ROOT', project), \
            patch.object(edit_context, 'after', return_value=event), \
            patch.object(source_card_provenance, 'capture_host_edits', return_value=['raw', 'card']), \
            patch.object(source_card_provenance, 'reconcile_pending', side_effect=reconcile), \
            patch.object(synthetic_experiment, 'shared_for', return_value=shared), \
            patch.object(posttool, '_edit_flow', return_value={'context': 'synthetic recall'}), \
            patch.object(sys, 'stdin', io.StringIO(json.dumps(payload))), redirect_stdout(output):
        assert posttool.main() == 0
    assert retried == [card], retried
    context = json.loads(output.getvalue())['hookSpecificOutput']['additionalContext']
    assert context == 'synthetic recall', context
    print(json.dumps({'mixed_card_retry': True, 'raw_recall_preserved': True}))
