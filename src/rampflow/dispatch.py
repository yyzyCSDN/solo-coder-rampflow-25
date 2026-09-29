from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from heapq import heappush,heappop
from typing import Iterable,Mapping,Callable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class DispatchJob:
    job_id:str
    kind:str
    location:str
    earliest:datetime
    latest:datetime
    duration_min:int
    priority:int=100
    capabilities:frozenset[str]=frozenset()
    dependency_ids:frozenset[str]=frozenset()
    def __post_init__(self):
        object.__setattr__(self,"earliest",utc(self.earliest));object.__setattr__(self,"latest",utc(self.latest))
        if self.latest<=self.earliest or self.duration_min<=0:raise ValueError("invalid dispatch job")

@dataclass(frozen=True)
class DispatchUnit:
    unit_id:str
    kind:str
    location:str
    capabilities:frozenset[str]
    speed_kph:float
    shift:Window

@dataclass(frozen=True)
class DispatchAssignment:
    job_id:str
    unit_id:str
    travel:Window|None
    service:Window
    distance_km:float
    cost:float

class Dispatcher:
    def __init__(self,units:Iterable[DispatchUnit],distance_km:Callable[[str,str],float]):
        self.units={u.unit_id:u for u in units}
        self.distance_km=distance_km
        self._use:dict[str,list[Window]]={u:[] for u in self.units}
        self._location={u.unit_id:u.location for u in self.units}
        self.assignments:dict[str,DispatchAssignment]={}

    def _topological(self,jobs:Mapping[str,DispatchJob])->tuple[str,...]:
        remaining={k:set(v.dependency_ids) for k,v in jobs.items()}
        for jid,deps in remaining.items():
            if deps-set(jobs):raise ValueError(f"missing dependency for {jid}")
        out=[];ready=sorted(j for j,d in remaining.items() if not d)
        while ready:
            jid=ready.pop(0);out.append(jid)
            for other in sorted(remaining):
                if jid in remaining[other]:
                    remaining[other].remove(jid)
                    if not remaining[other] and other not in out and other not in ready:ready.append(other);ready.sort()
        if len(out)!=len(jobs):raise ValueError("dispatch dependency cycle")
        return tuple(out)

    def _candidate(self,job:DispatchJob,unit:DispatchUnit,earliest:datetime)->DispatchAssignment|None:
        if unit.kind!=job.kind or not job.capabilities.issubset(unit.capabilities):return None
        distance=max(0.0,float(self.distance_km(self._location[unit.unit_id],job.location)))
        travel_min=int(distance/max(0.1,unit.speed_kph)*60+0.999)
        service_start=max(job.earliest,earliest)
        travel=None
        if travel_min:
            travel=Window(service_start,service_start+timedelta(minutes=travel_min))
            service_start=travel.end
        service=Window(service_start,service_start+timedelta(minutes=job.duration_min))
        full=service if travel is None else Window(travel.start,service.end)
        while True:
            hit=[w for w in self._use[unit.unit_id] if w.overlaps(full)]
            if not hit:break
            restart=max(w.end for w in hit)
            travel=Window(restart,restart+timedelta(minutes=travel_min)) if travel_min else None
            service_start=travel.end if travel else restart
            service=Window(service_start,service_start+timedelta(minutes=job.duration_min))
            full=service if travel is None else Window(travel.start,service.end)
        if service.end>job.latest or full.start<unit.shift.start or full.end>unit.shift.end:return None
        cost=distance*1.7+job.duration_min*0.3+(service.start-job.earliest).total_seconds()/60*0.2
        return DispatchAssignment(job.job_id,unit.unit_id,travel,service,distance,cost)

    def plan_batch(self,jobs:Iterable[DispatchJob])->tuple[DispatchAssignment,...]:
        jobmap={j.job_id:j for j in jobs}
        order=self._topological(jobmap)
        completion={}
        virtual_use={u:list(v) for u,v in self._use.items()}
        original=self._use
        self._use=virtual_use
        planned=[]
        try:
            for jid in sorted(order,key=lambda x:(jobmap[x].priority,order.index(x))):
                job=jobmap[jid]
                dep=max((completion[d] for d in job.dependency_ids),default=job.earliest)
                options=[]
                for unit in self.units.values():
                    candidate=self._candidate(job,unit,dep)
                    if candidate:options.append((candidate.cost,candidate.service.end,unit.unit_id,candidate))
                if not options:raise RuntimeError(f"no dispatch unit for {jid}")
                options.sort(key=lambda x:(x[0],x[1],x[2]))
                chosen=options[0][3]
                occupied=chosen.service if chosen.travel is None else Window(chosen.travel.start,chosen.service.end)
                self._use[chosen.unit_id].append(occupied)
                self._location[chosen.unit_id]=job.location
                completion[jid]=chosen.service.end
                planned.append(chosen)
        finally:
            self._use=original
        return tuple(planned)

    def commit_batch(self,assignments:Iterable[DispatchAssignment],jobs:Mapping[str,DispatchJob])->Decision:
        staged=[]
        for a in assignments:
            occupied=a.service if a.travel is None else Window(a.travel.start,a.service.end)
            if any(w.overlaps(occupied) for w in self._use[a.unit_id]):
                return Decision(False,"unit-stale","dispatch unit availability changed",{"unit":a.unit_id})
            staged.append((a,occupied))
        for a,occupied in staged:
            self._use[a.unit_id].append(occupied);self._location[a.unit_id]=jobs[a.job_id].location;self.assignments[a.job_id]=a
        return Decision(True,"ok","dispatch batch committed",{"count":len(staged)})
