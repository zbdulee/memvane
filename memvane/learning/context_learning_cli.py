#!/usr/bin/env python3
"""Explicit private context-learning operations; no automatic capture or worker."""
import argparse
import json
import sys

from memvane.learning.context_learning.contracts import digest
from memvane.learning.context_learning.jobs import cancel_job, job_status, label_job
from memvane.learning.context_learning.offline import approve_review, review_view, build_manifest, load_manifest
from memvane.learning.context_learning.dataset import read_private_json, export_dataset, inspect_export, evaluate_predictions
from memvane.learning.context_learning.store import canonical, database
from memvane.learning.context_learning.raw import export_laya_raw, check_laya_raw, evaluate_laya_predictions
from memvane.learning.context_learning.selection_gold import save_selection, load_selection
from memvane.learning.context_learning.local_cycle import LocalTrainer, VerifiedLayaTrainer, run_cycle, rollback
from memvane.learning.context_learning.schedule import run_due_cycle
from memvane.learning.context_learning.teacher_budget import budgeted_label_request, budgeted_review_request


def parser():
    p=argparse.ArgumentParser(description='Private request inspection and explicit provisional teacher/review operations.')
    p.add_argument('--state-dir',required=True,help='Existing owner-only private learning state directory.')
    subs=p.add_subparsers(dest='command',required=True)
    listing=subs.add_parser('list-requests');listing.add_argument('--limit',type=int,default=20)
    for name in ('inspect-request','inspect-labels','job-status','cancel-job'):
        sub=subs.add_parser(name);sub.add_argument('request_id')
    label=subs.add_parser('label-request');label.add_argument('request_id')
    label.add_argument('--host',required=True,choices=('claude','codex'))
    label.add_argument('--enable-teacher',action='store_true')
    label.add_argument('--accept-security-policy',action='store_true')
    label.add_argument('--optional-hooks-audited',action='store_true')
    label.add_argument('--disable-plugin-id',action='append',default=[])
    label.add_argument('--timeout',type=float,default=45)
    for name in ('budgeted-label-request','budgeted-second-opinion'):
        sub=subs.add_parser(name);sub.add_argument('request_id');sub.add_argument('attempt_id')
        sub.add_argument('--policy-json',required=True)
        sub.add_argument('--optional-hooks-audited',action='store_true')
        sub.add_argument('--accept-security-policy',action='store_true')
        sub.add_argument('--timeout',type=float,default=45)
    selection=subs.add_parser('review-selection');selection.add_argument('request_id')
    selection.add_argument('--selected-ids-json',required=True);selection.add_argument('--input-sha256',required=True)
    selection.add_argument('--reviewer-id',required=True);selection.add_argument('--reference',required=True)
    inspect_selection=subs.add_parser('inspect-selection');inspect_selection.add_argument('request_id')
    for name in ('run-local-cycle','run-due-cycle'):
        sub=subs.add_parser(name);sub.add_argument('cycle_id');sub.add_argument('version_id')
        sub.add_argument('--revisions-json',required=True);sub.add_argument('--trainer-argv-json',required=True)
        sub.add_argument('--incumbent-artifact',required=True)
        sub.add_argument('--max-seconds',type=int,default=900);sub.add_argument('--max-rss-mib',type=int,default=8192)
    subs.add_parser('rollback-local-checkpoint')
    base=subs.add_parser('prepare-local-laya-base')
    base.add_argument('--model-dir',required=True)
    review=subs.add_parser('review-label');review.add_argument('request_id');review.add_argument('candidate_id')
    review.add_argument('--approve',action='store_true')
    review.add_argument('--input-sha256');review.add_argument('--label-sha256')
    review.add_argument('--reviewer-id');review.add_argument('--reference')
    manifest=subs.add_parser('build-manifest');manifest.add_argument('version_id')
    manifest.add_argument('--assignments-json',required=True);manifest.add_argument('--revisions-json',required=True)
    manifest.add_argument('--parent-id')
    inspect=subs.add_parser('inspect-manifest');inspect.add_argument('version_id')
    for name in ('export-dataset','inspect-export','evaluate','export-laya-raw','check-laya-raw','evaluate-laya'):
        sub=subs.add_parser(name);sub.add_argument('version_id');sub.add_argument('--revisions-json',required=True)
        if name in ('evaluate','evaluate-laya'):
            sub.add_argument('--predictions-json',required=True)
            sub.add_argument('--split',choices=('validation','evaluation'),default='evaluation')
    return p


def execute(args):
    root=args.state_dir
    if args.command=='review-selection':
        return {'status':save_selection(root,args.request_id,read_private_json(args.selected_ids_json),
            input_sha256=args.input_sha256,reviewer_id=args.reviewer_id,reference=args.reference)}
    if args.command=='inspect-selection':return load_selection(root,args.request_id)
    if args.command=='rollback-local-checkpoint':return rollback(root)
    if args.command=='prepare-local-laya-base':
        from memvane.learning.context_learning.adapter import model_identity, prepare_base_artifact
        from pathlib import Path
        identity=model_identity(args.model_dir)
        destination=Path(root)/('laya-base-'+identity+'.artifact')
        if destination.exists():
            expected={'schema_version':1,'kind':'laya_base_head',
                'base_model_sha256':identity,'adapter_version':'qmd-laya-selection-v1'}
            if read_private_json(destination)!=expected:
                raise ValueError('existing_base_artifact_changed')
            return {'status':'already_prepared','artifact_path':str(destination),
                'base_model_sha256':identity}
        return prepare_base_artifact(args.model_dir,destination)
    if args.command in ('run-local-cycle','run-due-cycle'):
        argv=read_private_json(args.trainer_argv_json)
        from pathlib import Path
        adapter=Path(__file__).with_name('context_learning')/'adapter.py'
        trainer_type=(VerifiedLayaTrainer if isinstance(argv,list) and len(argv)==3 and
            Path(argv[1]).resolve()==adapter.resolve() else LocalTrainer)
        trainer=trainer_type(argv,max_seconds=args.max_seconds,
                             max_rss_mib=args.max_rss_mib)
        reader=lambda:read_private_json(args.revisions_json)
        if args.command=='run-local-cycle':return run_cycle(root,args.cycle_id,args.version_id,reader,trainer,
            incumbent_artifact=args.incumbent_artifact)
        return run_due_cycle(root,args.cycle_id,args.version_id,reader,trainer,
            incumbent_artifact=args.incumbent_artifact)
    if args.command in ('budgeted-label-request','budgeted-second-opinion'):
        policy=read_private_json(args.policy_json)
        opts={'optional_hooks_audited':args.optional_hooks_audited,
              'accept_security_policy':args.accept_security_policy,'timeout':args.timeout}
        if args.command=='budgeted-label-request':
            return budgeted_label_request(root,args.request_id,args.attempt_id,policy,**opts)
        return budgeted_review_request(root,args.request_id,args.attempt_id,policy,**opts)
    if args.command=='build-manifest':
        return build_manifest(root,args.version_id,read_private_json(args.assignments_json),
            read_private_json(args.revisions_json),parent_id=args.parent_id)
    if args.command=='inspect-manifest':return load_manifest(root,args.version_id)
    if args.command in ('export-dataset','inspect-export','evaluate','export-laya-raw','check-laya-raw','evaluate-laya'):
        revisions=read_private_json(args.revisions_json)
        if args.command=='export-dataset':return export_dataset(root,args.version_id,revisions)
        if args.command=='inspect-export':return inspect_export(root,args.version_id,revisions)
        if args.command=='export-laya-raw':return export_laya_raw(root,args.version_id,revisions)
        if args.command=='check-laya-raw':return check_laya_raw(root,args.version_id,revisions)
        if args.command=='evaluate-laya':
            return evaluate_laya_predictions(root,args.version_id,revisions,read_private_json(args.predictions_json),split=args.split)
        return evaluate_predictions(root,args.version_id,revisions,read_private_json(args.predictions_json),split=args.split)
    if args.command=='label-request':
        return label_job(root,args.request_id,enabled=args.enable_teacher,host=args.host,
            accept_security_policy=args.accept_security_policy,optional_hooks_audited=args.optional_hooks_audited,
            disabled_plugin_ids=args.disable_plugin_id,timeout=args.timeout)
    if args.command=='job-status':return job_status(root,args.request_id)
    if args.command=='cancel-job':return cancel_job(root,args.request_id)
    if args.command=='review-label':
        view=review_view(root,args.request_id,args.candidate_id)
        label=view['label']
        view['label_sha256']=digest(canonical(label)) if label is not None else None
        if not args.approve:return view
        if not all((args.input_sha256,args.label_sha256,args.reviewer_id,args.reference)):
            return {'status':'failed','reason':'explicit_review_fields_required'}
        if args.input_sha256!=view['input_sha256'] or args.label_sha256!=view['label_sha256']:
            return {'status':'failed','reason':'review_snapshot_mismatch'}
        status=approve_review(root,label,input_sha256=args.input_sha256,
            reviewer_id=args.reviewer_id,reference=args.reference)
        return {'status':status,'authentication':'trusted-local-caller-not-verified','truth':'not-certified'}
    with database(root) as db:
        if args.command=='list-requests':
            if not 1<=args.limit<=50:return {'status':'failed','reason':'invalid_limit'}
            rows=db.execute('SELECT body FROM samples ORDER BY request_id LIMIT ?', (args.limit,)).fetchall()
            return {'requests':[{'request_id':s['request_id'],'host':s['host'],'schema_version':s['schema_version'],
                'candidate_count':len(s['candidates']),'input_sha256':digest(canonical(s))}
                for s in (json.loads(row[0]) for row in rows)]}
        row=db.execute('SELECT body FROM samples WHERE request_id=?',(args.request_id,)).fetchone()
        if row is None:return {'status':'failed','reason':'unknown_or_deleted_request'}
        sample=json.loads(row[0])
        if args.command=='inspect-request':return {'sample':sample,'input_sha256':digest(canonical(sample))}
        ids=[c['candidate_id'] for c in sample['candidates']]
    return {'labels':[review_view(root,args.request_id,cid) for cid in ids]}


def main(argv=None):
    args=parser().parse_args(argv)
    try:result=execute(args)
    except KeyboardInterrupt:
        result={'status':'failed','reason':'cancelled'}
    except Exception:
        # Never serialize raw exception/provider/config content.
        result={'status':'failed','reason':'operation_rejected'}
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 1 if result.get('status')=='failed' else 0

if __name__=='__main__':raise SystemExit(main())
