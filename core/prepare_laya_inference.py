#!/usr/bin/env python3
"""Maintainer-only explicit inference preparation. Run with policy Python -I."""
import argparse
import json
from pathlib import Path
import sys
import time
sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
from context_learning.inference_service import _policy
from context_learning.inference_identity import prepare

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('state',type=Path)
    parser.add_argument('--max-seconds',type=int,default=120)
    args=parser.parse_args()
    if not 1 <= args.max_seconds <= 120: parser.error('max-seconds must be 1..120')
    _,policy=_policy(args.state,'active-checkpoint.json',deadline=time.monotonic()+args.max_seconds)
    receipt=prepare(args.state,policy['trainerArgv'],max_seconds=args.max_seconds,
                    max_rss_mib=policy['maxRssMib'])
    print(json.dumps({'status':'prepared','schema':receipt['schema'],
                      'namespace':receipt['namespace'],'checks':receipt['checks']}))
if __name__=='__main__':main()
