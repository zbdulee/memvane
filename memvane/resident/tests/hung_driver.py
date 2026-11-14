"""Isolated synthetic resident with a stalled cold load and one owned child."""
import os
from pathlib import Path
import subprocess
import sys
import threading

from memvane.resident import resident_service as svc
from memvane.resident.tests.support import manifest_for


def main(base):
    base = Path(base)
    root = base / 'service'
    project = base / 'project'
    project.mkdir(mode=0o700)
    manifest = manifest_for(base / 'install.sock')
    decision = {'projectRoot': str(project), 'installId': 'synthetic',
                'journalPath': str(base / 'journal'), 'scopeId': 'synthetic',
                'storePath': str(base / 'store'), 'storeIncarnation': 'synthetic',
                'vectorProfile': {'embeddingFingerprint': manifest['embeddingFingerprint']}}
    started = threading.Event()

    def start_worker(_manifest, _admit):
        child = subprocess.Popen([sys.executable, '-B', '-c', 'import sys; sys.stdin.read()'],
                                 stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        (base / 'child.pid').write_text(str(child.pid))
        started.set()
        threading.Event().wait()

    svc.register(str(project), root=root)
    service = svc.Service(root, bundle={'version': '1.0.0'},
                          prove=lambda _project: (decision, {'providerManifest': manifest}, 'selected'),
                          attest=lambda *_args: None, start_worker=start_worker,
                          admit=lambda: True, tick=.05)
    if not service.acquire(wait=5):
        return 75
    service.start()
    if not started.wait(15):
        return 76
    service.begin_drain()
    (base / 'draining').write_text('1')
    threading.Event().wait()


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
