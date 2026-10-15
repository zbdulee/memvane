"""Hash-pinned local optimizer boundary receipts, never a publish operation."""
from pathlib import Path
import os
from .contracts import digest
from .dataset import read_private_json
from .local_cycle import _atomic_json, _hash, _safe_file
from .store import canonical
from . import resident


def request_identity(request):
    # Admission may shrink a microbatch on resume; effective groups stay fixed.
    return digest(canonical({k:v for k,v in request.items() if k!='training_limits'}))


def completion(request):
    path=Path(request['artifact_path']).with_suffix('.completed.json')
    if not path.exists() and not path.is_symlink():return None
    row=read_private_json(path)
    if row!={'schema':1,'request_sha256':request_identity(request),'artifact_sha256':_hash(request['artifact_path'])}:
        raise ValueError('learning_completion_changed')
    return row


def record_completion(request):
    path=Path(request['artifact_path']).with_suffix('.completed.json')
    _atomic_json(path,{'schema':1,'request_sha256':request_identity(request),
                       'artifact_sha256':_hash(request['artifact_path'])})
    _sync(path.parent)


def _sync(folder):
    fd=os.open(folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


class UpdateCursor:
    """Two head slots: an interrupted write never overwrites the committed head.

    SGD is deliberately pinned to momentum-free lr=.01, with no optimizer
    tensors. Any future stateful optimizer needs a distinct protocol.
    """
    def __init__(self,request,groups):
        artifact=Path(request['artifact_path'])
        resident._private_state(artifact.parent)
        self.root=artifact.parent/'resume'
        if not self.root.exists():self.root.mkdir(mode=0o700)
        resident._private_state(self.root)
        self.path=self.root/'cursor.json'
        self.identity=request_identity(request)
        self.groups_sha=digest(canonical(groups))
        self.maximum=min(len(groups),128)
        if not 1<=len(groups)<=128:raise ValueError('learning_update_budget_exceeded')
        self.step=0;self.saved=None
        if self.path.exists() or self.path.is_symlink():
            row=read_private_json(self.path)
            if (row.get('schema')!=1 or row.get('request_sha256')!=self.identity
                    or row.get('groups_sha256')!=self.groups_sha
                    or row.get('optimizer')!={'name':'SGD','lr':.01,'momentum':0}
                    or type(row.get('next_group')) is not int
                    or not 1<=row['next_group']<=self.maximum
                    or row.get('head_slot') not in (0,1)
                    or row.get('head_sha256')!=_hash(self.root/f"head-{row['head_slot']}.artifact")):
                raise ValueError('learning_cursor_changed')
            self.step=row['next_group'];self.saved=row

    def head(self):
        return self.root/f"head-{self.saved['head_slot']}.artifact" if self.saved else None

    def commit(self,next_group,save_head,rng):
        if next_group!=self.step+1 or next_group>self.maximum:raise ValueError('learning_cursor_step_conflict')
        if (not isinstance(rng,dict) or set(rng)!={'cpu','mps'}
                or any(v is not None and (not isinstance(v,list) or len(v)>65536
                    or any(type(x)is not int or not 0<=x<=255 for x in v)) for v in rng.values())):
            raise ValueError('learning_rng_invalid')
        slot=next_group%2;head=self.root/f'head-{slot}.artifact';pending=self.root/f'head-{slot}.pending'
        if pending.exists():_safe_file(pending);pending.unlink()
        save_head(pending)
        _safe_file(pending)
        with pending.open('rb') as stream:os.fsync(stream.fileno())
        if head.exists() or head.is_symlink():_safe_file(head)
        os.replace(pending,head);_sync(self.root)
        row={'schema':1,'request_sha256':self.identity,'groups_sha256':self.groups_sha,
             'next_group':next_group,'head_slot':slot,'head_sha256':_hash(head),
             'optimizer':{'name':'SGD','lr':.01,'momentum':0},'rng':rng}
        _atomic_json(self.path,row);_sync(self.root)
        self.saved=row;self.step=next_group
