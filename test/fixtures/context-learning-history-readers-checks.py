"""Runtime-authorization ledger reads must follow an active history selection.

After activation the legacy shared/private files are fenced and frozen; new
writes land in the canonical history. A reader that opens the legacy path
directly reads stale data. The committed activation state (state + pointer)
is written here the same way activate() writes it, because activate()
refuses whenever `home` equals Path.home() and these readers resolve the
account through Path.home(). Everything lives in a disposable home.

isolated_train/derived_split are deliberately not covered: their preflights
refuse an active history (`*_history_activation_unsupported`) and their
sources are SHA-pinned by approved mappings.
"""
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from unittest.mock import patch

from context_learning.contracts import digest, request
from context_learning.synthetic_experiment import PLANNED_BUNDLE_NAME, SHARED_NAME
from context_learning import synthetic_experiment
from context_learning import history_activation
from context_learning.history_activation import (
    _atomic_file, _write_state, selected_database, writer_access, PROTOCOL)
from context_learning.history_migration import prepare
from context_learning import runtime_authorization as auth
from context_learning import shared as shared_module
from context_learning.shared import _db
from context_learning.storage_layout import register_project, root as learning_root
from context_learning.store import canonical, database


with tempfile.TemporaryDirectory(prefix='qmd-history-readers-') as name:
    home = Path(name).resolve()
    project = home / 'novel'
    story = project / PLANNED_BUNDLE_NAME
    story.mkdir(parents=True)
    shared = project / SHARED_NAME
    shared.mkdir(mode=0o700)
    private_parent = project / 'private'
    private_parent.mkdir(mode=0o700)
    private = private_parent / digest(str(project))
    private.mkdir(mode=0o700)
    settings = project / '.auto-context/settings.json'
    settings.parent.mkdir()
    wiki_collection = 'fantasy-20261008-wiki'
    settings.write_text(canonical({
        'indexing': True, 'collections': [wiki_collection],
        'collectionPaths': {wiki_collection:
                            str(Path(story.name) / '.auto-context/wiki')},
        'collectionRoles': {wiki_collection: 'wiki'},
        'contextLearning': {'capture': True, 'stateRoot': str(private_parent)},
        'sharedLearning': {'enabled': True, 'root': str(shared)}}))
    consent_dir = shared / 'consents'
    consent_dir.mkdir(mode=0o700)
    consent = consent_dir / (digest(str(project)) + '.json')
    consent.write_text(canonical({'schema': 'qmd-shared-consent-v1',
                                  'projectRoot': str(project),
                                  'sharedRoot': str(shared), 'capture': True,
                                  'localTraining': True,
                                  'externalTeacher': False}))
    consent.chmod(0o600)
    project_id = register_project(project, story_root=story,
                                  display_name='Approved story', home=home)
    request_id = 'old-request-id'
    sample = request({'prompt': 'Which old card?'}, [{
        'candidate_id': 'card-1', 'revision_sha256': 'e'*64,
        'eligible': True, 'excerpt': 'body'}], host='codex',
        request_id=request_id)
    body = canonical(sample)
    with _db(shared, home=home) as db:
        db.execute('INSERT INTO cases VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (digest(str(project)), str(project), request_id, digest(body),
                    body, canonical({'card-1': 'body'}), 'b'*64, 1.0,
                    'train', '[]', 'active', 'gold', '{}', '{}',
                    canonical({'selected_ids': ['card-1']}), None, None))
    with database(private, home=home) as db:
        db.execute('INSERT INTO samples VALUES (?,?,?)',
                   (request_id, sample['schema_version'], body))
        db.execute('INSERT INTO compact_inputs VALUES (?,?,?)',
                   (request_id, 'card-1', canonical({'body_text': 'body',
                    'body_sha256': digest('body'), 'source_sha256': 'e'*64})))
    shared_db = shared / shared_module.DATABASE_NAME
    private_db = private / 'learning.sqlite3'
    generation = prepare(project_id, shared_db, private_db, home=home)['generation']

    # Commit the activation exactly as activate() does at its end.
    manifest_path = learning_root(home) / 'migrations' / generation / 'prepared.json'
    # Readers route on the pointer and state phase; the seal digests are
    # verified only by recover()/rollback(), so placeholders suffice here.
    _write_state(generation, 'active', home=home, seal={
        'modes': [shared_db.stat().st_mode & 0o777,
                  private_db.stat().st_mode & 0o777],
        'schema': ['0'*64, '0'*64]})
    _atomic_file(history_activation._pointer(home), {
        'schema': PROTOCOL, 'generation': generation, 'project_id': project_id,
        'manifest_sha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        'source_shared': str(shared_db), 'source_private': str(private_db)})
    history = manifest_path.with_name('history.sqlite3')
    assert selected_database(shared_db, home=home) == history
    assert selected_database(private_db, home=home) == history

    with patch.object(Path, 'home', return_value=home):
        # runtime_authorization: a ticket written after activation exists only
        # in the history and must enter the frozen baseline.
        cutoff_ms = 4_102_444_800_000
        cohort = {'schema': auth.COHORT_SCHEMA, 'id': 'fresh-edit-cohort-v1',
                  'notBeforeUnixMs': cutoff_ms,
                  'trainEditPrefixes': [
                      '01_Settings/qmd-prospective/tr-a/',
                      '02_Characters/qmd-prospective/tr-b/',
                      '03_Plot/qmd-prospective/tr-c/',
                      '01_Settings/qmd-prospective/tr-d/',
                      '01_Settings/qmd-prospective/tr-e/'],
                  'validationEditPrefixes': [
                      '01_Settings/qmd-prospective/va-a/',
                      '02_Characters/qmd-prospective/va-b/',
                      '03_Plot/qmd-prospective/va-c/',
                      '01_Settings/qmd-prospective/va-d/',
                      '02_Characters/qmd-prospective/va-e/'],
                  'minTrainCases': 5, 'minQualityTrainCases': 100,
                  'minTrainFamilies': 5, 'minValidationCases': 50,
                  'minValidationFamilies': 5, 'minEmptyValidation': 10,
                  'baselineRawPaths': []}
        new_path = cohort['trainEditPrefixes'][0] + 'after-activation.md'
        with writer_access(shared_db, home=home) as path:
            assert path == history
            with sqlite3.connect(path) as db:
                db.execute(
                    "INSERT INTO edit_tickets (ticket_id,project_root,story_root,"
                    "relative_path,episode,approval_sha256,parent_approval_sha256,"
                    "document_sha256,source_side,source_start,segment_start,"
                    "segment_end,segment_sha256,event_json,session_id,turn_id,"
                    "tool_use_id,operation,created_at,expires_at) VALUES ("
                    + ','.join('?' for _ in range(20)) + ')',
                    ('history-ticket', str(project), str(story), new_path, 0,
                     'a', 'b', 'c', 'after', 0, 0, 1, 'd', '{}', 's', 't', 'u',
                     'write', 1.0, 2.0))
        with patch.object(synthetic_experiment, 'project_for',
                          return_value=project):
            assert auth.baseline_raw_paths(story, cohort, shared) == [new_path]

    # No reader in these modules may open a learning ledger directly.
    core = Path(__file__).resolve().parents[2] / 'core/context_learning'
    assert 'sqlite3.connect(' not in (core / 'runtime_authorization.py').read_text()
print('ok')
