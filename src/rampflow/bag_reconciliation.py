from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from enum import Enum
from typing import Iterable,Mapping
from .timeutil import utc

class BagState(str,Enum):
    ACCEPTED='accepted';SCREENED='screened';SORTED='sorted';LOADED='loaded';OFFLOADED='offloaded';TRANSFER='transfer';MISSING='missing'

@dataclass(frozen=True)
class Bag:
    tag:str
    passenger_id:str
    flight_id:str
    state:BagState
    last_seen_at:datetime
    location:str
    container_id:str|None=None
    sequence:int=0
    def __post_init__(self):object.__setattr__(self,'last_seen_at',utc(self.last_seen_at))

@dataclass(frozen=True)
class Scan:
    scan_id:str;tag:str;occurred_at:datetime;location:str;action:str;flight_id:str|None=None;container_id:str|None=None
    def __post_init__(self):object.__setattr__(self,'occurred_at',utc(self.occurred_at))

class BagReconciler:
    def __init__(self,max_late_scan_minutes:int=30):
        self.max_late=timedelta(minutes=max_late_scan_minutes);self.bags:dict[str,Bag]={};self._seen_scans:set[str]=set();self._history:dict[str,list[Scan]]={}

    def register(self,bag:Bag)->None:
        if bag.tag in self.bags:raise ValueError('duplicate bag')
        self.bags[bag.tag]=bag;self._history[bag.tag]=[]

    def ingest(self,scan:Scan)->Bag:
        if scan.scan_id in self._seen_scans:return self.bags[scan.tag]
        bag=self.bags[scan.tag]
        if scan.occurred_at<bag.last_seen_at-self.max_late:raise RuntimeError('scan outside late-arrival tolerance')
        history=self._history[scan.tag]+[scan];history.sort(key=lambda s:(s.occurred_at,s.scan_id))
        rebuilt=replace(bag,state=BagState.ACCEPTED,last_seen_at=min([bag.last_seen_at]+[s.occurred_at for s in history]),sequence=0)
        allowed={
            BagState.ACCEPTED:{'screen':BagState.SCREENED,'transfer':BagState.TRANSFER,'missing':BagState.MISSING},
            BagState.SCREENED:{'sort':BagState.SORTED,'missing':BagState.MISSING},
            BagState.SORTED:{'load':BagState.LOADED,'missing':BagState.MISSING},
            BagState.LOADED:{'offload':BagState.OFFLOADED,'missing':BagState.MISSING},
            BagState.OFFLOADED:{'sort':BagState.SORTED,'transfer':BagState.TRANSFER},
            BagState.TRANSFER:{'screen':BagState.SCREENED,'sort':BagState.SORTED,'missing':BagState.MISSING},
            BagState.MISSING:{'screen':BagState.SCREENED,'sort':BagState.SORTED,'load':BagState.LOADED},
        }
        for s in history:
            nxt=allowed.get(rebuilt.state,{}).get(s.action)
            if nxt is None:raise RuntimeError(f'invalid bag transition {rebuilt.state}:{s.action}')
            flight=s.flight_id or rebuilt.flight_id
            if rebuilt.state==BagState.LOADED and s.action=='offload' and s.flight_id and s.flight_id!=rebuilt.flight_id:raise RuntimeError('offload flight mismatch')
            rebuilt=replace(rebuilt,state=nxt,last_seen_at=s.occurred_at,location=s.location,flight_id=flight,container_id=s.container_id if s.container_id is not None else rebuilt.container_id,sequence=rebuilt.sequence+1)
        self._history[scan.tag]=history;self._seen_scans.add(scan.scan_id);self.bags[scan.tag]=rebuilt;return rebuilt

    def reconcile_departure(self,flight_id:str,boarded_passengers:Iterable[str])->Mapping[str,tuple[str,...]]:
        boarded=set(boarded_passengers);loaded=[];must_offload=[];missing=[]
        for bag in self.bags.values():
            if bag.flight_id!=flight_id:continue
            if bag.state==BagState.LOADED:
                loaded.append(bag.tag)
                if bag.passenger_id not in boarded:must_offload.append(bag.tag)
            elif bag.passenger_id in boarded and bag.state not in {BagState.OFFLOADED}:missing.append(bag.tag)
        return {'loaded':tuple(sorted(loaded)),'must_offload':tuple(sorted(must_offload)),'missing_for_boarded':tuple(sorted(missing))}

    def transfer_deadline(self,tag:str,departure:datetime,minimum_connect_min:int)->datetime:
        bag=self.bags[tag]
        departure=utc(departure)
        if bag.state not in {BagState.TRANSFER,BagState.SCREENED,BagState.SORTED}:raise RuntimeError('bag not in transfer flow')
        return departure-timedelta(minutes=minimum_connect_min)
