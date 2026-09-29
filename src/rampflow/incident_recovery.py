from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from enum import Enum
from typing import Iterable,Mapping
from .timeutil import utc

class IncidentState(str,Enum):OPEN='open';CONTAINED='contained';RECOVERING='recovering';RESOLVED='resolved';CANCELLED='cancelled'

@dataclass(frozen=True)
class RecoveryAction:
    action_id:str
    domain:str
    duration_min:int
    dependencies:frozenset[str]=frozenset()
    resource:str|None=None
    reversible:bool=True

@dataclass(frozen=True)
class Incident:
    incident_id:str
    opened_at:datetime
    severity:int
    state:IncidentState
    actions:tuple[RecoveryAction,...]
    completed:frozenset[str]=frozenset()
    failed:frozenset[str]=frozenset()
    version:int=1
    def __post_init__(self):object.__setattr__(self,'opened_at',utc(self.opened_at))

class RecoveryCoordinator:
    def __init__(self):self.incidents:dict[str,Incident]={};self._resource_until:dict[str,datetime]={}

    def open(self,incident:Incident)->None:
        if incident.incident_id in self.incidents:raise ValueError('duplicate incident')
        ids={a.action_id for a in incident.actions}
        if len(ids)!=len(incident.actions):raise ValueError('duplicate recovery action')
        if any(not a.dependencies<=ids for a in incident.actions):raise ValueError('unknown action dependency')
        self._topological(incident.actions);self.incidents[incident.incident_id]=incident

    def _topological(self,actions:Iterable[RecoveryAction])->tuple[str,...]:
        amap={a.action_id:a for a in actions};done=set();out=[]
        while len(done)<len(amap):
            ready=sorted(k for k,a in amap.items() if k not in done and a.dependencies<=done)
            if not ready:raise ValueError('recovery action cycle')
            out.extend(ready);done.update(ready)
        return tuple(out)

    def contain(self,incident_id:str)->Incident:
        inc=self.incidents[incident_id]
        if inc.state!=IncidentState.OPEN:raise RuntimeError('incident not open')
        inc=replace(inc,state=IncidentState.CONTAINED,version=inc.version+1);self.incidents[incident_id]=inc;return inc

    def ready_actions(self,incident_id:str,at:datetime)->tuple[RecoveryAction,...]:
        inc=self.incidents[incident_id];at=utc(at)
        if inc.state not in {IncidentState.CONTAINED,IncidentState.RECOVERING}:return ()
        out=[]
        for action in inc.actions:
            if action.action_id in inc.completed|inc.failed:continue
            if not action.dependencies<=inc.completed:continue
            if action.resource and self._resource_until.get(action.resource,at)>at:continue
            out.append(action)
        return tuple(sorted(out,key=lambda a:(a.domain,a.action_id)))

    def start(self,incident_id:str,action_id:str,at:datetime)->datetime:
        at=utc(at);inc=self.incidents[incident_id]
        ready={a.action_id:a for a in self.ready_actions(incident_id,at)}
        if action_id not in ready:raise RuntimeError('action not ready')
        action=ready[action_id];finish=at+timedelta(minutes=action.duration_min)
        if action.resource:self._resource_until[action.resource]=finish
        if inc.state==IncidentState.CONTAINED:
            inc=replace(inc,state=IncidentState.RECOVERING,version=inc.version+1);self.incidents[incident_id]=inc
        return finish

    def complete(self,incident_id:str,action_id:str,success:bool)->Incident:
        inc=self.incidents[incident_id]
        action=next((a for a in inc.actions if a.action_id==action_id),None)
        if action is None:raise KeyError(action_id)
        if success:inc=replace(inc,completed=inc.completed|{action_id},version=inc.version+1)
        else:inc=replace(inc,failed=inc.failed|{action_id},version=inc.version+1)
        if len(inc.completed)==len(inc.actions):inc=replace(inc,state=IncidentState.RESOLVED,version=inc.version+1)
        self.incidents[incident_id]=inc;return inc

    def rollback_order(self,incident_id:str)->tuple[str,...]:
        inc=self.incidents[incident_id];amap={a.action_id:a for a in inc.actions}
        order=self._topological(inc.actions)
        return tuple(aid for aid in reversed(order) if aid in inc.completed and amap[aid].reversible)
