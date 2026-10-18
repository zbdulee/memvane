"""Fixed per-kind wake handlers; runtime_work owns the queue, lease and retry.

Each handler revalidates setup/ownership/authorization itself right before its
side effect and returns True only when the wake may be ACKed. Owner modules are
imported inside the handler, so importing this table never loads learning code.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import runtime_wake_policy as wake_policy


def index(row):
    result=subprocess.run(['bash',str(Path(__file__).resolve().parents[1]/'backend/index_worker.sh')],
        stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
        env={**os.environ,'QMD_RUNTIME_DISPATCH':'1'},check=False)
    from dirty_queue import queue_path
    path=queue_path()
    return result.returncode==0 and (not path.exists() or path.stat().st_size==0)


def update(row):
    import update_hook_queue
    update_hook_queue.worker(row['target'])
    return not (update_hook_queue._private_queue()/(row['target']+'.job.json')).exists()


def laya_sync(row):
    from laya_product_sync_worker import run
    result=run(Path(row['target']))
    return result['state'] in ('indexed','unchanged')


def laya_compile(row):
    from wiki_selected_laya import run
    result=run(Path(row['target']))
    return result['state']=='drained'


def learning(row):
    import config
    import setup_guard
    from context_learning import shared
    project=Path(row['target'])
    if not project.is_dir():return True
    if setup_guard.status(project)['status']!='ready':return False
    cfg=config.find_project_config(str(project))['config'];root=shared._safe_root(cfg,project)
    if root is None:return True
    # Resource retries must not launch a teacher or create fresh captures.
    from context_learning.history_lifecycle import worker_lease
    with worker_lease():
        if setup_guard.status(project)['status']!='ready':return False
        result=shared.run_training_pending(root,project)
    return wake_policy.handler_done(result['status'])


def auto(row):
    import context_learning_auto
    result=context_learning_auto.run(Path(row['target']))
    return wake_policy.handler_done(result['status'])


def cleanup(row):
    import setup_guard,qmd_route
    project=Path(row['target'])
    if not project.is_dir():return True
    if setup_guard.status(project)['status']!='ready':return False
    route=qmd_route.project_paths(project)
    result=subprocess.run([sys.executable,'-B',str(Path(__file__).with_name('orphan_reclaim.py')),
        '--cwd',str(project),'--json'],stdin=subprocess.DEVNULL,capture_output=True,text=True,
        env={**os.environ,**{key:route[key] for key in qmd_route.PATH_KEYS},'QMD_RUNTIME_DISPATCH':'1'},check=False)
    if result.returncode:return False
    return wake_policy.cleanup_done(json.loads(result.stdout))


HANDLERS={'index':index,'update':update,'laya-sync':laya_sync,'laya-compile':laya_compile,
          'learning':learning,'auto':auto,'cleanup':cleanup}


def dispatch(row):
    """Fixed handlers revalidate setup/ownership/authorization themselves."""
    handler=HANDLERS.get(row['kind'])
    if handler is None:raise ValueError('invalid_runtime_handler')
    return handler(row)
