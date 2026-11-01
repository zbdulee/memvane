"""Real-shaped selected installs for out-of-project edit routing checks.

Each install has minimal ``{name, skipPaths}`` settings in ``.memvane/``, a v2
route record bound to the settings identity, the private reviewed route, an
install journal (sqlite) activated on that route, a sealed production
attestation, the fresh-wiki ledger and marker, and real Laya index/claim-queue
databases, under ``$HOME/.local/state/memvane/<install id>``. Only the bound
plugin bundle read (``inspect_full_plugin_candidate``) is replaced, through
``BundleReader``; no plugin cache is built.
"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import mock

FIXTURES = Path(__file__).resolve().parent
sys.path.insert(0, str(FIXTURES.parents[1] / 'core'))
sys.path.insert(1, str(FIXTURES))
import config  # noqa: E402
import laya_fresh_wiki_origin as fresh  # noqa: E402
import laya_index_host_route as host_route  # noqa: E402
import laya_install_journal as journal  # noqa: E402
import memvane_paths  # noqa: E402
import project_dir  # noqa: E402
from laya_index_a import LayaIndexA  # noqa: E402
from laya_index_claim_queue import ClaimQueue  # noqa: E402
from laya_product_bundle import PRODUCTION_ATTESTATION_SCHEMA  # noqa: E402
from laya_selected_project import canon, sha, vector_profile  # noqa: E402


def private_write(path, raw):
    path.write_bytes(raw)
    path.chmod(0o600)


def make_install(home, project, name, install_id, scope_id, *, activate=True):
    """One selected (or, without ``activate``, candidate-ready) install."""
    home, project = Path(home), Path(project)
    folder = project / project_dir.CURRENT
    (folder / project_dir.WIKI).mkdir(parents=True)
    settings = folder / 'settings.json'
    settings.write_text(json.dumps({'name': name, 'skipPaths': []}, indent=2) + '\n')
    base = memvane_paths.state_base(home)
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    state = base / install_id
    state.mkdir(mode=0o700)
    plugin = home / 'plugins' / name
    plugin.mkdir(parents=True)
    user_qmd = home / 'user-qmd'
    user_qmd.mkdir(exist_ok=True)
    journal_path = state / 'install-journal.sqlite'
    db, queue = state / 'index.sqlite', state / 'claims.sqlite'
    with closing(LayaIndexA(db, create=True)) as index:
        incarnation = index.incarnation()
    with closing(ClaimQueue.create(queue, index_incarnation=incarnation)):
        pass
    for store in ('catalog.sqlite', 'binding.sqlite'):
        (state / store).write_bytes(b'')
    raw_name, wiki_name = config.minimal_collection_names(name)
    collections = [{'name': raw_name, 'role': 'raw', 'sourceRoot': str(project)},
                   {'name': wiki_name, 'role': 'wiki',
                    'sourceRoot': str(folder / project_dir.WIKI)}]
    proof = {'status': 'exact_commit_full_plugin_candidate_consistent',
             'pluginRoot': str(plugin), 'sourceCommit': 'c' * 40,
             'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
             'vectorProfile': vector_profile()}
    route = {'schema': host_route.SCHEMA_V2, 'status': 'active',
             'projectRoot': str(project),
             'settingsSha256': host_route.settings_identity_digest(project, collections),
             'dbPath': str(db), 'queuePath': str(queue), 'dbIncarnation': incarnation,
             'scopeId': scope_id, 'collections': collections,
             'pluginRoot': str(plugin), 'journalPath': str(journal_path),
             'installId': install_id, 'generation': 'g-' + '4' * 32,
             'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
             'vectorProfile': vector_profile()}
    route_raw = canon(route)
    private_write(folder / host_route.RECORD, route_raw)
    private_write(state / 'reviewed-route.json', route_raw)
    attestation = {'schema': PRODUCTION_ATTESTATION_SCHEMA, 'sourceCommit': 'c' * 40,
                   'bundleManifestSha256': 'd' * 64, 'ownershipManifestSha256': 'e' * 64,
                   'journalPath': str(journal_path), 'vectorProfile': vector_profile(),
                   'providerManifest': {'providerPath': str(plugin / 'p.py')},
                   'dependencyClosure': []}
    attestation_raw = journal._canon(attestation).encode('utf-8')
    private_write(state / journal.PRODUCTION_ATTESTATION, attestation_raw)
    with mock.patch.object(journal, 'inspect_full_plugin_candidate', return_value=proof):
        created = journal.create_journal(journal_path, proof, route_raw,
                                         user_qmd_roots=[str(user_qmd)])
    install = SimpleNamespace(
        name=name, project=project, folder=folder, settings=settings, state=state,
        plugin=plugin, journal=journal_path, db=db, queue=queue, route=route,
        route_raw=route_raw, proof=proof, install_id=install_id, scope_id=scope_id,
        attestation=state / journal.PRODUCTION_ATTESTATION,
        raw_name=raw_name, wiki_name=wiki_name)
    if not activate:
        return install
    identity_rows = [[str(plugin / 'p.py'), 1, 2, 3, 4, 5]]
    with mock.patch.object(journal, '_inspect_identity', return_value=proof), \
            mock.patch.object(journal, '_bind_attestation',
                              return_value=(sha(attestation_raw), identity_rows)):
        selected = journal.cas_activate(journal_path, created['epoch'], route_raw)
    assert selected['state'] == 'active', selected
    identity = lambda path: [path.stat().st_dev, path.stat().st_ino]
    ledger = {
        'schema': fresh.SCHEMA, 'epoch': 3, 'phase': 'active',
        'projectRoot': str(project), 'journalPath': str(journal_path),
        'routeSha256': sha(route_raw), 'collections': collections,
        'candidateDb': str(db), 'queueDb': str(queue),
        'catalogDb': str(state / 'catalog.sqlite'),
        'bindingStore': str(state / 'binding.sqlite'),
        'scope': scope_id, 'indexGeneration': 'g-' + '7' * 32 + '-b' + '8' * 64,
        'sourceManifest': '9' * 64, 'bindingDigest': '8' * 64,
        'catalogScanId': 'a' * 32, 'catalogRawDigest': 'b' * 64,
        'vectorProfileId': vector_profile()['profileId'], 'vectorRows': 1,
        'candidateIncarnation': incarnation,
        'attestationSha256': sha(attestation_raw),
        'providerFileIdentitySha256': selected['providerFileIdentitySha256'],
        'candidateIdentity': identity(db), 'queueIdentity': identity(queue),
        'catalogIdentity': identity(state / 'catalog.sqlite'),
        'bindingStoreIdentity': identity(state / 'binding.sqlite')}
    fresh._write(state / fresh.LEDGER, ledger, replace=False)
    fresh._write(folder / fresh.MARKER, fresh._marker(ledger), replace=False)
    return install


def legacy_settings(install, **overrides):
    """The legacy-shape settings with the same identity as the minimal file."""
    value = {'name': install.name, 'indexing': True,
             'collections': [install.raw_name, install.wiki_name],
             'collectionPaths': {install.raw_name: '.',
                                 install.wiki_name: project_dir.CURRENT + '/' + project_dir.WIKI},
             'collectionRoles': {install.raw_name: 'raw', install.wiki_name: 'wiki'},
             'skipPaths': [], 'recallStrategy': 'wikiOnly', 'compile': {'mode': 'off'}}
    value.update(overrides)
    return value


class BundleReader:
    """``inspect_full_plugin_candidate`` over the installs' sealed proofs.

    A plugin root in ``drifted`` reads as a replaced bundle.
    """

    def __init__(self, installs):
        self.proofs = {install.proof['pluginRoot']: install.proof for install in installs}
        self.drifted = set()

    def add(self, install):
        self.proofs[install.proof['pluginRoot']] = install.proof

    def __call__(self, root):
        proof = self.proofs.get(str(root))
        if proof is None:
            raise journal.BundleEvidenceError('bundle_unavailable')
        if str(root) in self.drifted:
            return {**proof, 'bundleManifestSha256': 'f' * 64}
        return dict(proof)
