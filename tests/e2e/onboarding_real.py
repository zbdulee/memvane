"""Real-model onboarding through the process entry points (temporary HOME only).

Provisions the embedding runtime from a local GGUF, onboards projects with
``entry/setup.py`` and asks ``hooks/run-hook`` for recall. Run by
``onboarding-real.test.mjs`` under ``--kind model-test``; every path lives
under the temporary HOME and MEMVANE_TEST_RUNTIME_ROOT.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HOME = Path(os.environ['HOME'])
MODEL = os.environ['MEMVANE_TEST_MODEL_FILE']
DIMENSION = '768'  # embeddinggemma-300M
BUILD_SECONDS = float(os.environ.get('MEMVANE_TEST_BUILD_SECONDS', '900'))
sys.path.insert(0, str(ROOT))


def stage(name, **fields):
    print(json.dumps({'stage': name, **fields}, sort_keys=True, default=str), flush=True)


def need(condition, message, **fields):
    if not condition:
        stage('failed', message=message, **fields)
        raise SystemExit(1)


def setup(*args, root=ROOT, ok=(0,)):
    proc = subprocess.run([sys.executable, '-B', str(root / 'entry/setup.py'), *args],
                          capture_output=True, text=True, timeout=1800)
    lines = proc.stdout.strip().splitlines()
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        result = {'unparsed': proc.stdout[-2000:]}
    need(proc.returncode in ok, 'setup_' + args[0], code=proc.returncode, result=result,
         stderr=proc.stderr[-4000:])
    return result


def hook(action, payload, *, engine='claude', root=ROOT):
    proc = subprocess.run([str(root / 'hooks/run-hook'), action, engine], input=json.dumps(payload),
                          capture_output=True, text=True, timeout=60, cwd=payload.get('cwd'))
    return proc.returncode, proc.stdout, proc.stderr


def git(project, *args):
    subprocess.run(['git', '-C', str(project), *args], check=True, capture_output=True, text=True)


def card_text(title, marker, revision_line):
    from memvane.wiki.card.api import card_body_hash
    text = (f'---\ntitle: "{title}"\nstatus: verified\ncreatedBy: memvane\n'
            f'sourceRevisions:\n  - {revision_line}\n---\n# {title}\n\n{marker}\n')
    return text.replace('createdBy: memvane\n',
                        'createdBy: memvane\nverifiedBodyHash: ' + card_body_hash(text) + '\n', 1)


def make_project(name, cards):
    """A git project with minimal settings, one raw note and ``cards`` verified cards."""
    import hashlib
    from memvane.settings.config import minimal_collection_names
    from memvane.wiki.card.api import dump_source_revisions
    project = HOME / name
    wiki = project / '.memvane' / 'wiki' / 'concepts'
    wiki.mkdir(parents=True)
    (project / '.memvane' / 'settings.json').write_text(json.dumps({'name': name}) + '\n')
    (project / '.gitignore').write_text('.memvane/route.json\n.memvane/selection.json\n')
    raw = project / 'notes.md'
    raw.write_text('# Field notes\n\nThe emerald heron nests beside the reed pond every spring.\n')
    git(project, 'init', '-q')
    git(project, 'add', '-A')
    git(project, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'notes')
    revision = {'kind': 'file', 'path': 'notes.md', 'collection': minimal_collection_names(name)[0],
                'sha256': hashlib.sha256(raw.read_bytes()).hexdigest(), 'size': raw.stat().st_size,
                'mtimeNs': raw.stat().st_mtime_ns}
    line = dump_source_revisions([revision])[0]
    topics = ['heron', 'lantern', 'glacier', 'violin', 'saffron', 'compass', 'orchard', 'quartz',
              'harbor', 'falcon', 'meadow', 'ember', 'cobalt', 'willow', 'canyon', 'pepper',
              'beacon', 'tundra', 'marble', 'thistle', 'anchor', 'nebula', 'juniper', 'copper',
              'lagoon', 'raven', 'cedar', 'prism', 'dune', 'mosaic']
    for topic in topics[:cards]:
        marker = f'The {topic} protocol keeps marker token zq{topic}x for recall checks.'
        (wiki / f'{topic}.md').write_text(card_text(f'{topic.title()} protocol', marker, line))
    if cards:
        git(project, 'add', '-A')
        git(project, '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', 'commit', '-qm', 'cards')
    return project


def onboard(project):
    plan = setup('plan-fresh-wiki-origin', '--project', str(project))
    need(plan.get('proposalSha256'), 'plan_without_proposal', plan=plan)
    install = secrets.token_hex(16)
    state = HOME / '.local/state/memvane/v1/installs' / install
    review = setup('review-index-provider', '--project', str(project), '--state-root', str(state),
                   '--provider-dimension', DIMENSION)
    need(review.get('proposalSha256'), 'review_without_proposal', review=review)
    prepared = setup('prepare-fresh-wiki-origin', '--project', str(project), '--state-root', str(state),
                     '--install-id', install, '--accepted-origin-sha256', plan['proposalSha256'],
                     '--accepted-provider-sha256', review['proposalSha256'],
                     '--provider-dimension', DIMENSION)
    stage('prepared', project=project.name, result=prepared)
    deadline = time.monotonic() + BUILD_SECONDS
    status = None
    while time.monotonic() < deadline:
        status = setup('index-build-status', '--project', str(project), '--state-root', str(state),
                       ok=(0, 1, 75))
        if status.get('phase') == 'ready' or status.get('buildPhase') == 'ready' or status.get('ready') is True:
            break
        time.sleep(2)
    stage('build_status', project=project.name, result=status)
    need(status and (status.get('phase') == 'ready' or status.get('buildPhase') == 'ready'
                     or status.get('ready') is True), 'initial_build_not_ready', result=status)
    finalized = setup('finalize-index-origin', '--project', str(project), '--state-root', str(state))
    stage('finalized', project=project.name, result=finalized)
    activated = setup('activate-index-origin', '--project', str(project), '--state-root', str(state),
                      '--approve-index-origin-activation')
    stage('activated', project=project.name, result=activated)
    return state


def events(state, event):
    path = state / 'events.jsonl'
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [row for row in rows if row.get('event') == event]


def recall(project, prompt, **kwargs):  # noqa: D103 - hook kwargs pass through
    code, out, err = hook('recall', {'prompt': prompt, 'cwd': str(project), 'session_id': 's5-onboarding',
                                     'hook_event_name': 'UserPromptSubmit'}, **kwargs)
    context = ''
    if out.strip():
        context = json.loads(out).get('hookSpecificOutput', {}).get('additionalContext', '')
    return code, context, err


def plugin_copy(name, *, version=None, provider_suffix=None):
    """A copy of this plugin tree (as a host cache would hold another build)."""
    import shutil
    listed = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-co', '--exclude-standard', '-z'],
                            capture_output=True, check=True).stdout.decode().split('\0')
    target = HOME / name
    for rel in filter(None, listed):
        source = ROOT / rel
        if source.is_file() and not source.is_symlink():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / rel)
    if version is not None:
        manifest = json.loads((target / 'plugin.json').read_text())
        manifest['version'] = version
        (target / 'plugin.json').write_text(json.dumps(manifest, indent=2) + '\n')
    if provider_suffix is not None:
        provider = target / 'memvane/provider/adapters/local_embed_provider.py'
        provider.write_text(provider.read_text() + provider_suffix)
    return target


def session_start(project, *, root=ROOT):
    return hook('update-queued', {'cwd': str(project), 'session_id': 's5-onboarding',
                                  'hook_event_name': 'SessionStart', 'source': 'startup'}, root=root)


def journal_row(state):
    import sqlite3
    with sqlite3.connect((state / 'journal.sqlite').as_uri() + '?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        return dict(db.execute('SELECT epoch,state,max_plugin_version,writer_floor FROM selection').fetchone())


def writer_wake(project, *, root=ROOT):
    proc = subprocess.run([sys.executable, '-B', str(root / 'entry/run.py'), 'wake', 'laya-sync', str(project)],
                          capture_output=True, text=True, timeout=600)
    return proc.returncode, proc.stdout[-1500:], proc.stderr[-1500:]


def mismatch_scenarios(project, state):
    """D4 major mismatch, D5 provider change, and the heal->warm version record."""
    baseline = journal_row(state)
    stage('journal_before', row=baseline)
    for label, root in (('major', plugin_copy('plugin-v2', version='2.0.0')),
                        ('provider', plugin_copy('plugin-provider', provider_suffix='\n# changed provider build\n'))):
        code, context, err = recall(project, 'what does the glacier protocol keep?', root=root)
        stage(label + '_recall', code=code, context=context[:300], stderr=err[-300:])
        need(code == 0 and 'zqglacierx' not in context, label + '_recall_served', context=context[:300])
        code, out, err = session_start(project, root=root)
        stage(label + '_session_start', code=code, out=out[-800:], stderr=err[-300:])
        need(code == 0 and out.strip(), label + '_session_start_silent', out=out, stderr=err)
        code, out, err = writer_wake(project, root=root)
        after = journal_row(state)
        stage(label + '_writer', code=code, out=out, stderr=err, row=after)
        need(after['epoch'] == baseline['epoch'], label + '_writer_changed_journal', row=after)
    newer = plugin_copy('plugin-v101', version='1.0.1')
    code, out, err = session_start(project, root=newer)
    stage('newer_session_start', code=code, out=out[-800:], stderr=err[-300:])
    need(code == 0, 'newer_session_start_failed', stderr=err)
    deadline = time.monotonic() + 300
    row = journal_row(state)
    while time.monotonic() < deadline and row['max_plugin_version'] != '1.0.1':
        time.sleep(2)
        row = journal_row(state)
    stage('newer_recorded', row=row)
    need(row['max_plugin_version'] == '1.0.1', 'heal_warm_version_not_recorded', row=row)
    code, context, err = recall(project, 'what does the glacier protocol keep?')
    stage('older_host_recall_after_newer', code=code, context=context[:200])
    need(code == 0 and 'zqglacierx' in context, 'older_host_lost_recall', context=context[:300])


def stop_owned_processes():
    """SIGTERM processes this run started detached (their command names this HOME)."""
    import signal
    # -ww: without it ps cuts the command line and the HOME path is not seen.
    listing = subprocess.run(['ps', '-axww', '-o', 'pid=,command='], capture_output=True, text=True).stdout
    mine = [int(line.split(None, 1)[0]) for line in listing.splitlines()
            if str(HOME) in line and int(line.split(None, 1)[0]) != os.getpid()]
    for pid in mine:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    stage('stopped', pids=mine)


def main():
    import signal

    def stopped(signum, _frame):
        # The guard stops a run with SIGTERM before SIGKILL; the detached
        # resident service is outside its process tree.
        stop_owned_processes()
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, stopped)
    try:
        scenarios()
    finally:
        stop_owned_processes()


def scenarios():
    provisioned = setup('provision-index-runtime', '--approve-index-runtime-download',
                        '--provider-model-file', MODEL)
    stage('provisioned', result={k: provisioned.get(k) for k in ('status', 'runtimeId', 'generation')})

    empty = make_project('empty', 0)
    empty_state = onboard(empty)
    code, context, err = recall(empty, 'emerald heron reed pond')
    stage('recall_empty', code=code, context=context[:400], stderr=err[-400:],
          events=events(empty_state, 'recall')[-1:])
    need(code == 0, 'recall_empty_exit', stderr=err)

    many = make_project('many', 30)
    many_state = onboard(many)
    code, context, err = recall(many, 'what does the glacier protocol keep?')
    rows = events(many_state, 'recall')
    stage('recall_many', code=code, context=context[:600], stderr=err[-400:], events=rows[-1:])
    need(code == 0 and 'zqglacierx' in context, 'recall_many_missing_card', context=context[:600])
    need(rows and not rows[-1].get('vector_fallback'), 'recall_many_not_vector', event=rows[-1:] )
    mismatch_scenarios(many, many_state)
    stage('done')


if __name__ == '__main__':
    main()
