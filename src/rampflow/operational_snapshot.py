from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from json import dumps,loads
from typing import Mapping,Any
from .timeutil import utc

@dataclass(frozen=True)
class OperationalSnapshot:
    snapshot_id:str
    created_at:datetime
    generation:int
    payload:Mapping[str,object]
    digest:str

class SnapshotCodec:
    @staticmethod
    def _canonical(payload:Mapping[str,object])->bytes:
        return dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False,default=str).encode('utf-8')

    def create(self,snapshot_id:str,generation:int,payload:Mapping[str,object],at:datetime)->OperationalSnapshot:
        at=utc(at);raw=self._canonical(payload);digest=sha256(raw).hexdigest()
        return OperationalSnapshot(snapshot_id,at,generation,dict(payload),digest)

    def verify(self,snapshot:OperationalSnapshot)->bool:
        return sha256(self._canonical(snapshot.payload)).hexdigest()==snapshot.digest

    def encode(self,snapshot:OperationalSnapshot)->bytes:
        if not self.verify(snapshot):raise ValueError('snapshot digest mismatch')
        envelope={'snapshot_id':snapshot.snapshot_id,'created_at':snapshot.created_at.isoformat(),'generation':snapshot.generation,'payload':snapshot.payload,'digest':snapshot.digest}
        return self._canonical(envelope)

    def decode(self,data:bytes)->OperationalSnapshot:
        obj=loads(data.decode('utf-8'))
        snap=OperationalSnapshot(obj['snapshot_id'],utc(datetime.fromisoformat(obj['created_at'])),int(obj['generation']),obj['payload'],obj['digest'])
        if not self.verify(snap):raise ValueError('snapshot digest mismatch')
        return snap

class SnapshotRegistry:
    def __init__(self):self._items:dict[str,OperationalSnapshot]={}
    def add(self,snapshot:OperationalSnapshot)->None:
        if snapshot.snapshot_id in self._items:raise ValueError('duplicate snapshot id')
        self._items[snapshot.snapshot_id]=snapshot
    def latest(self)->OperationalSnapshot:
        if not self._items:raise KeyError('no snapshots')
        return max(self._items.values(),key=lambda x:(x.generation,x.created_at,x.snapshot_id))
    def compare(self,first:str,second:str)->Mapping[str,tuple[object|None,object|None]]:
        a=self._items[first].payload;b=self._items[second].payload;out={}
        for key in sorted(set(a)|set(b)):
            if a.get(key)!=b.get(key):out[key]=(a.get(key),b.get(key))
        return out
