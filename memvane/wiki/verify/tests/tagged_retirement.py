"""Prepare a legacy retirement using only the archived e8 writer in PYTHONPATH."""
import hashlib
import json
import sys
from pathlib import Path

from memvane.wiki.verify import pending_ownership as ledger
from memvane.wiki.verify.wiki_verify_worker import _cleanup_job_key

assert Path(ledger.__file__).resolve().is_relative_to(Path.cwd()), ledger.__file__
root, queue, wiki = map(Path, sys.argv[1:4])
job, config = map(json.loads, sys.argv[4:])
state = ledger.load(queue)
assert ledger.prepare(root, config, wiki, state, job) is None
plan = state['retirements'][job['pendingPath']]
plan['swap'] = '.verify-swap-a1b2c3d4'
ledger.store(queue, state)
operation = _cleanup_job_key(job) + plan['sha256'] + plan['swap']
print('retirement_' + hashlib.sha256(operation.encode()).hexdigest())
