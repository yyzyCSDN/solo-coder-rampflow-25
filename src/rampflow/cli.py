from __future__ import annotations
import argparse,json
from datetime import datetime,timezone
from .operational_snapshot import SnapshotCodec

def _parser():
    p=argparse.ArgumentParser(prog='rampflow',description='Airport ground-operations analysis utilities')
    sub=p.add_subparsers(dest='command',required=True)
    h=sub.add_parser('health');h.add_argument('--json',action='store_true')
    s=sub.add_parser('snapshot-digest');s.add_argument('json_file');s.add_argument('--generation',type=int,default=0)
    return p

def main(argv=None):
    args=_parser().parse_args(argv)
    if args.command=='health':
        payload={'status':'ok','service':'rampflow','runtime_dependencies':0}
        print(json.dumps(payload,sort_keys=True) if args.json else 'rampflow: ok')
        return 0
    if args.command=='snapshot-digest':
        with open(args.json_file,'r',encoding='utf-8') as f:payload=json.load(f)
        snap=SnapshotCodec().create('cli',args.generation,payload,datetime.now(timezone.utc))
        print(snap.digest);return 0
    return 2

if __name__=='__main__':raise SystemExit(main())
