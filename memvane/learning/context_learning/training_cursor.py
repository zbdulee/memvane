"""Hash-pinned local optimizer boundary receipts, never a publish operation."""
from pathlib import Path
import os
import re
import tempfile
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
    artifact=_safe_file(request['artifact_path'])
    with artifact.open('rb') as stream:os.fsync(stream.fileno())
    path=Path(request['artifact_path']).with_suffix('.completed.json')
    _atomic_json(path,{'schema':1,'request_sha256':request_identity(request),
                       'artifact_sha256':_hash(request['artifact_path'])})
    _sync(path.parent)


def _sync(folder):
    fd=os.open(folder,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(fd)
    finally:os.close(fd)


def _valid_rng(rng):
    return (isinstance(rng,dict) and set(rng)=={'cpu','mps'}
            and isinstance(rng['cpu'],list) and bool(rng['cpu'])
            and all(v is None or (isinstance(v,list) and 0<len(v)<=65536
                and all(type(x)is int and 0<=x<=255 for x in v)) for v in rng.values()))


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
        self.maximum=len(groups)
        if not 1<=len(groups)<=7500:raise ValueError('learning_update_budget_exceeded')
        self.step=0;self.saved=None
        if self.path.exists() or self.path.is_symlink():
            row=read_private_json(self.path)
            if (row.get('schema')!=1 or row.get('request_sha256')!=self.identity
                    or row.get('groups_sha256')!=self.groups_sha
                    or row.get('optimizer')!={'name':'SGD','lr':.01,'momentum':0}
                    or type(row.get('next_group')) is not int
                    or not 1<=row['next_group']<=self.maximum
                    or row.get('total_groups',self.maximum)!=self.maximum
                    or row.get('head_slot') not in (0,1) or row['head_slot']!=row['next_group']%2
                    or not _valid_rng(row.get('rng'))
                    or row.get('head_sha256')!=_hash(self.root/f"head-{row['head_slot']}.artifact")):
                raise ValueError('learning_cursor_changed')
            self.step=row['next_group'];self.saved=row
        # Independently pin the actual adapter-generated plan before any update.
        # Recovery must not trust a cursor's own (possibly damaged) group count.
        plan={'schema':1,'request_sha256':self.identity,'groups_sha256':self.groups_sha,
              'total_groups':self.maximum}
        plan_path=self.root/'group-plan.json'
        try:fd=os.open(plan_path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        except FileExistsError:
            if read_private_json(plan_path)!=plan:raise ValueError('learning_cursor_changed')
        else:
            with os.fdopen(fd,'w') as stream:
                stream.write(canonical(plan)+'\n');stream.flush();os.fsync(stream.fileno())
            _sync(self.root)

    def head(self):
        return self.root/f"head-{self.saved['head_slot']}.artifact" if self.saved else None

    def commit(self,next_group,save_head,rng):
        if next_group!=self.step+1 or next_group>self.maximum:raise ValueError('learning_cursor_step_conflict')
        if not _valid_rng(rng):
            raise ValueError('learning_rng_invalid')
        slot=next_group%2;head=self.root/f'head-{slot}.artifact';pending=self.root/f'head-{slot}.pending'
        if pending.exists() or pending.is_symlink():_safe_file(pending);pending.unlink()
        save_head(pending)
        _safe_file(pending)
        with pending.open('rb') as stream:os.fsync(stream.fileno())
        if head.exists() or head.is_symlink():_safe_file(head)
        os.replace(pending,head);_sync(self.root)
        row={'schema':1,'request_sha256':self.identity,'groups_sha256':self.groups_sha,
             'next_group':next_group,'total_groups':self.maximum,'head_slot':slot,'head_sha256':_hash(head),
             'optimizer':{'name':'SGD','lr':.01,'momentum':0},'rng':rng}
        _atomic_json(self.path,row);_sync(self.root)
        self.saved=row;self.step=next_group


def recover_completed(request):
    """Complete only an exact fully committed cursor, without model execution.

    Unknown existing final bytes are preserved for review. An interrupted new
    copy leaves only a private scratch file; the prior cursor/head never changes.
    Callers retain their existing job/trainer ownership and resource admission.
    """
    artifact=Path(request['artifact_path']);path=artifact.parent/'resume/cursor.json'
    if not path.exists() and not path.is_symlink():return False
    row=read_private_json(path)
    plan_path=path.parent/'group-plan.json'
    # Old cursors must pass through the adapter's actual group validation first.
    if not plan_path.exists() and not plan_path.is_symlink():return False
    plan=read_private_json(plan_path)
    if (row.get('schema')!=1 or row.get('request_sha256')!=request_identity(request)
            or row.get('optimizer')!={'name':'SGD','lr':.01,'momentum':0}
            or type(row.get('total_groups')) is not int or not 1<=row['total_groups']<=7500
            or type(row.get('next_group')) is not int or row['next_group']!=row['total_groups']
            or row.get('head_slot')!=row['next_group']%2 or not _valid_rng(row.get('rng'))
            or not re.fullmatch('[a-f0-9]{64}',row.get('groups_sha256',''))
            or plan!={'schema':1,'request_sha256':request_identity(request),
                      'groups_sha256':row.get('groups_sha256'),'total_groups':row['total_groups']}):return False
    head=_safe_file(path.parent/f"head-{row['head_slot']}.artifact")
    if _hash(head)!=row.get('head_sha256'):raise ValueError('learning_cursor_changed')
    if artifact.exists() or artifact.is_symlink():
        if _hash(artifact)!=row['head_sha256']:raise ValueError('learning_completion_uncertain')
    else:
        resident._private_state(artifact.parent)
        fd,name=tempfile.mkstemp(prefix='.head-final-',dir=artifact.parent)
        try:
            with os.fdopen(fd,'wb') as output,head.open('rb') as source:
                for block in iter(lambda:source.read(1024*1024),b''):output.write(block)
                output.flush();os.fsync(output.fileno())
            if _hash(name)!=row['head_sha256']:raise ValueError('learning_cursor_changed')
            if artifact.exists() or artifact.is_symlink():raise ValueError('learning_completion_uncertain')
            os.replace(name,artifact);_sync(artifact.parent)
        finally:Path(name).unlink(missing_ok=True)
    record_completion(request);return True


def run_updates(cursor,groups,guard,update,save_head,rng,*,per_run=128):
    """Only fully committed optimizer groups advance the durable cursor."""
    losses=[]
    start=cursor.step
    for index in range(start,len(groups)):
        with guard.step() as admission:
            loss=update(groups[index],admission['microbatch_size'])
            cursor.commit(index+1,save_head,rng())
            losses.append(loss)
        if index+1-start>=per_run and cursor.step<len(groups):
            raise ValueError('learning_yield')
    return losses
