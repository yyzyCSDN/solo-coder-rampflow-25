from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class Badge:
    badge_id:str
    holder:str
    zones:frozenset[str]
    valid:Window
    escort_capable:bool=False
    suspended:bool=False
    max_escortees:int=0

@dataclass(frozen=True)
class AccessRequest:
    request_id:str
    person_id:str
    zone:str
    at:datetime
    badge_id:str|None=None
    escort_badge_id:str|None=None
    purpose:str=""
    sterile_item_id:str|None=None
    def __post_init__(self):
        object.__setattr__(self,"at",utc(self.at))

@dataclass(frozen=True)
class CustodyEvent:
    item_id:str
    holder:str
    at:datetime
    zone:str
    action:str
    seal:str
    def __post_init__(self):
        object.__setattr__(self,"at",utc(self.at))

class SecurityManager:
    def __init__(self,badges:Iterable[Badge],zone_transitions:Mapping[tuple[str,str],frozenset[str]]):
        self.badges={b.badge_id:b for b in badges}
        self.transitions=dict(zone_transitions)
        self._escort_load:dict[str,list[tuple[Window,str]]]={}
        self.custody:dict[str,list[CustodyEvent]]={}

    def _badge_valid(self,badge_id:str,zone:str,at:datetime)->bool:
        b=self.badges.get(badge_id)
        return bool(b and not b.suspended and zone in b.zones and b.valid.contains(at,end_inclusive=True))

    def access(self,req:AccessRequest,escort_window_min:int=20)->Decision:
        if req.badge_id and self._badge_valid(req.badge_id,req.zone,req.at):
            return Decision(True,"badge","direct badge access")
        if not req.escort_badge_id:
            return Decision(False,"denied","no valid badge or escort")
        escort=self.badges.get(req.escort_badge_id)
        if escort is None or escort.suspended or not escort.escort_capable:
            return Decision(False,"escort","escort badge invalid")
        if req.zone not in escort.zones or not escort.valid.contains(req.at,end_inclusive=True):
            return Decision(False,"escort-zone","escort lacks zone authority")
        w=Window(req.at,req.at+timedelta(minutes=escort_window_min))
        active=[x for x in self._escort_load.get(escort.badge_id,()) if x[0].overlaps(w)]
        if len(active)>=escort.max_escortees:
            return Decision(False,"escort-capacity","escort capacity exhausted",{"active":len(active)})
        self._escort_load.setdefault(escort.badge_id,[]).append((w,req.person_id))
        return Decision(True,"escorted","escorted access granted",{"escort":escort.holder})

    def release_escort(self,escort_badge_id:str,person_id:str,at:datetime)->int:
        t=utc(at);rows=self._escort_load.get(escort_badge_id,[])
        updated=[];changed=0
        for w,p in rows:
            if p==person_id and w.contains(t):
                if w.start<t:updated.append((Window(w.start,t),p))
                changed+=1
            else:updated.append((w,p))
        self._escort_load[escort_badge_id]=updated
        return changed

    def record_custody(self,event:CustodyEvent)->Decision:
        rows=self.custody.setdefault(event.item_id,[])
        if rows:
            last=rows[-1]
            if event.at<last.at:return Decision(False,"time-order","custody event precedes latest event")
            allowed=self.transitions.get((last.zone,event.zone),frozenset())
            if last.zone!=event.zone and event.action not in allowed:
                return Decision(False,"zone-transition","custody transition not authorized",{"from":last.zone,"to":event.zone})
            if event.seal!=last.seal and event.action not in {"reseal","inspection"}:
                return Decision(False,"seal","seal changed without authorized action")
            if event.action=="handoff" and event.holder==last.holder:
                return Decision(False,"handoff","handoff must change holder")
        rows.append(event)
        return Decision(True,"recorded","custody event recorded",{"count":len(rows)})

    def verify_chain(self,item_id:str)->Decision:
        rows=self.custody.get(item_id,[])
        if not rows:return Decision(False,"missing","no custody chain")
        for a,b in zip(rows,rows[1:]):
            if b.at<a.at:return Decision(False,"time-order","custody timestamps not monotonic")
            if b.zone!=a.zone:
                allowed=self.transitions.get((a.zone,b.zone),frozenset())
                if b.action not in allowed:return Decision(False,"zone-transition","unauthorized custody zone transition")
            if b.seal!=a.seal and b.action not in {"reseal","inspection"}:
                return Decision(False,"seal","unauthorized seal mutation")
        return Decision(True,"complete","custody chain valid",{"events":len(rows)})
