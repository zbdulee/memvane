"""The learned head reaches recall through the real inference service.

LP-2's driver (``lp2.py``) on an install whose learning runtime is the real
one: the enrollment prepares and warms the selector itself (attestation,
inference manifest, receipt), the trainer is the one the product builds from
the policy, and a candidate is applied through the product transport to an
inference service running the real interpreter and model (``apply_prepare``,
``apply_activate``). The path ends with a recall through
``hooks/run-hook``: its row's ``selectorCheckpoint`` must name the applied
artifact.

The product binds the bundle that runs it to a host's plugin cache under the
account home, so the first stage copies this checkout's tracked files into the
fixture HOME's Claude cache and runs the second stage from there. What stays a
stand-in: the host CLIs (LP-1's deterministic teacher, and the plugin listing
in this process, the hook children and the service),
the lookup of the installed runtime (the interpreter and model come from the
environment), and the start of the inference service, which is the
learning-only one (``inference_service._ensure_direct``) instead of the
per-user resident that hosts it in an install. The driver is the wake (no
durable wake is registered in a fixture run), so the service's monitor does
not observe the resources between passes: retried passes follow each other
without a pause and are the observations the recovery latch needs.

A run is continued like LP-2's, from the directory on the command line::

    QMD_LAYA_LIVE=1 QMD_LAYA_PYTHON=<laya python> QMD_LAYA_MODEL=<model dir> \\
      npm run dev:guard -- --kind model-test --max-seconds 1800 -- \\
      python3 -B -m memvane.learning.tests.model.resident_loop <directory> --seconds 1500

``<directory>/lp2-record.jsonl`` is LP-2's record. The call exits 0 while the
path has not ended and when it ended with the recall on the applied artifact;
1 when it ended any other way.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest import mock

HOME_MARKER = 'resident-home.json'
SERVICE_START_SECONDS = 120
SERVICE_STOP_SECONDS = 30
# Not part of an installed bundle's code: the project's own wiki and the documents.
NOT_BUNDLED = ('.memvane/', 'docs/')


def stage(directory, checkout):
    """The fixture HOME with this checkout as its Claude plugin cache; ``(home, bundle)``."""
    saved = directory / HOME_MARKER
    if saved.exists():
        row = json.loads(saved.read_text(encoding='utf8'))
        return Path(row['home']), Path(row['bundle'])
    base = directory / 'h'
    base.mkdir(mode=0o700, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix='h', dir=str(base))).resolve()
    os.chmod(home, 0o700)
    version = json.loads((checkout / 'package.json').read_text(encoding='utf8'))['version']
    bundle = home / '.claude/plugins/cache/memvane-marketplace/memvane' / version
    listed = subprocess.run(['git', '-C', str(checkout), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], capture_output=True, check=True)
    for name in listed.stdout.decode('utf8').split(chr(0)):
        if not name or name.startswith(NOT_BUNDLED) or not (checkout / name).is_file():
            continue
        target = bundle / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(checkout / name), str(target))
    head = subprocess.run(['git', '-C', str(checkout), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    dirty = subprocess.run(['git', '-C', str(checkout), 'status', '--porcelain'], capture_output=True, text=True)
    saved.write_text(json.dumps({'home': str(home), 'bundle': str(bundle), 'commit': head.stdout.strip(),
                                 'modified': len(dirty.stdout.splitlines())}), encoding='utf8')
    return home, bundle


def fixture_class(home):
    """LP-2's fixture on the real runtime, built in the HOME ``stage`` made."""
    from memvane.learning.context_learning import automatic_staging, general_teacher, inference_service
    from memvane.learning.tests.model import lp2
    from memvane.learning.tests.old_bundle_121 import ROOT, SITECUSTOMIZE

    python, model = os.environ['QMD_LAYA_PYTHON'], os.environ['QMD_LAYA_MODEL']

    class ResidentFixture(lp2.Lp2Fixture):
        services = []
        whole_bundle = True

        def setUp(self):
            made, used = tempfile.mkdtemp, []

            def first(*args, **kwargs):
                # The fixture HOME exists already: the bundle that runs this process is in it.
                if not used and kwargs.get('prefix') == 'h':
                    used.append(True)
                    return str(home)
                return made(*args, **kwargs)
            with mock.patch.object(tempfile, 'mkdtemp', first):
                super().setUp()

        def attach(self, saved):
            super().attach(saved)
            self.learning_runtime()

        def bundle(self, host, version, marker):
            return ROOT

        def learning_runtime(self):
            self.model, self.runtime_file = Path(model), Path(python)

        def inference_manifest(self):
            pass  # The selector preparation wrote it.

        def warm_receipt(self):
            pass  # The selector warm wrote it.

        def host_stand_ins(self):
            from contextlib import ExitStack
            self.select(host='claude', version=self.version, marker='A')
            stack = ExitStack()
            self.addCleanup(stack.close)
            self.patches = stack
            for target, kwargs in (
                ('memvane.learning.runtime_activation.ensure_pending', {}),
                ('memvane.learning.context_learning.host_inventory._project_listing',
                 {'side_effect': self.listing}),
                ('memvane.learning.context_learning.adapter_binding._codex_installed',
                 {'side_effect': lambda *a: dict(self.codex)}),
                ('memvane.learning.context_learning.selector_heal.official_runtime',
                 {'return_value': (python, model)}),
            ):
                stack.enter_context(mock.patch(target, **kwargs))
            began = time.monotonic()
            inference_service._ensure_direct(inference_service.service_root(create=True), python,
                                             time.monotonic() + SERVICE_START_SECONDS, spawn=self.spawn)
            self.started = round(time.monotonic() - began, 1)

        def spawn(self, argv, **kwargs):
            # The service validates an application against the host's plugin listing, which
            # is a stand-in in every process of this fixture: its bootstrap applies the same
            # one the hook children carry (it runs isolated, so not through the environment).
            config = json.dumps({'home': str(self.home), 'running': str(self.running), 'codex': self.codex,
                                 'claude': self.claude, 'whole_bundle': True})
            prefix = ('import os,sys;sys.path.insert(0,%r);os.environ["MV121_CHILD"]=%r;exec(%r);'
                      % (str(ROOT), config, SITECUSTOMIZE))
            at = argv.index('-c') + 1
            argv = [*argv[:at], prefix + argv[at], *argv[at + 1:]]
            child = subprocess.Popen(argv, **kwargs)
            self.services.append(child)
            return child

        def worker_stand_ins(self):
            real = general_teacher.process_pending_default
            build = automatic_staging._trainer
            fixture = self

            def trainer(*args, **kwargs):
                made = build(*args, **kwargs)
                call = made.call

                def recorded(mode, request, result_path, *, cwd):
                    note = {'trainer': mode, 'cases': len(request.get('train') or request.get('cases') or ()),
                            'base': mode != 'train' and request['artifact_path'] == fixture.policy()['baseArtifact']}
                    began = time.monotonic()
                    try:
                        answer = call(mode, request, result_path, cwd=cwd)
                    except Exception as exc:
                        fixture.note(dict(note, seconds=round(time.monotonic() - began, 1), error=str(exc)[:80],
                                          errorCode=getattr(made, 'last_error_code', None)))
                        raise
                    fixture.note(dict(note, seconds=round(time.monotonic() - began, 1)))
                    return answer
                made.call = recorded
                return made
            for target, name, value in (
                    (general_teacher, 'process_pending_default',
                     lambda root, project, policy, **k: real(root, project, policy, environ=self.environ, **k)),
                    (automatic_staging, '_trainer', trainer)):
                self.patches.enter_context(mock.patch.object(target, name, value))

        def summary(self, end):
            row = super().summary(end)
            pointer = self.pointer()
            row['serviceStartSeconds'] = getattr(self, 'started', None)
            row['recallOnApplied'] = bool(row['applied'] and row['recall']['selectorCheckpoint']
                                          == pointer['artifact_sha256'][:12])
            return row

        def stop_services(self):
            left = 0
            for child in self.services:
                child.terminate()
                try:
                    child.wait(timeout=SERVICE_STOP_SECONDS)
                except subprocess.TimeoutExpired:
                    left += 1
            return left

    return ResidentFixture


def run(directory, home, seconds):
    from memvane.learning.tests.model import lp2
    chosen = fixture_class(home)
    try:
        # No pause between retried passes: after a resource deferral the admission's recovery
        # latch needs observations at most ``RECOVERY_MAX_GAP`` seconds apart. In an install the
        # service's monitor makes them while a learning wake is registered; here the passes do.
        return lp2.run(directory, seconds=seconds, fixture_class=chosen, pause=lambda _seconds: None)
    finally:
        left = chosen.stop_services(chosen)
        if left:
            print(json.dumps({'servicesLeftAfterSigterm': left}), file=sys.stderr, flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('directory')
    parser.add_argument('--seconds', type=float, default=1500)
    parser.add_argument('--home', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if os.environ.get('QMD_LAYA_LIVE') != '1':
        raise SystemExit('set QMD_LAYA_LIVE=1 with QMD_LAYA_PYTHON and QMD_LAYA_MODEL')
    directory = Path(args.directory).resolve()
    if args.home is None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        home, bundle = stage(directory, Path(__file__).resolve().parents[4])
        env = dict(os.environ, PYTHONPATH=str(bundle), PYTHONDONTWRITEBYTECODE='1')
        done = subprocess.run([sys.executable, '-B', '-m', __spec__.name, str(directory), '--seconds',
                               str(args.seconds), '--home', str(home)], cwd=str(bundle), env=env)
        return done.returncode
    base = directory / 'h'
    # The fixture run's environment (``tests.support.case.require_fixture_env``).
    os.environ.update(HOME=str(base), QMD_SETUP_GUARD_FIXTURE='1',
                      MEMVANE_TEST_RUNTIME_ROOT=str(base / 'rt'), PYTHONDONTWRITEBYTECODE='1')
    (base / 'rt').mkdir(mode=0o700, exist_ok=True)
    summary = run(directory, Path(args.home), args.seconds)
    print(json.dumps(summary if summary is not None else {'summary': None, 'continue': str(directory)},
                     sort_keys=True))
    return 0 if summary is None or summary.get('recallOnApplied') else 1


if __name__ == '__main__':
    sys.exit(main())
