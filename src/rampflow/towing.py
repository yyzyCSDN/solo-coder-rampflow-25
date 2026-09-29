from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from heapq import heappush,heappop
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class Tug:
    tug_id:str
    max_tow_kg:float
    compatible_gears:frozenset[str]
    speed_m_s:float
    battery_kwh:float
    reserve_kwh:float
    available:bool=True

@dataclass(frozen=True)
class TowSegment:
    segment_id:str
    src:str
    dst:str
    length_m:float
    max_tow_kg:float
    one_way:bool=True
    conflict_group:str=""

@dataclass(frozen=True)
class TowJob:
    job_id:str
    aircraft_reg:str
    weight_kg:float
    gear_type:str
    from_node:str
    to_node:str
    earliest:datetime
    latest:datetime
    attach_min:int=4
    detach_min:int=3
    def __post_init__(self):
        a,b=utc(self.earliest),utc(self.latest)
        if b<=a:raise ValueError("invalid tow window")
        object.__setattr__(self,"earliest",a);object.__setattr__(self,"latest",b)

@dataclass(frozen=True)
class TowPlan:
    job_id:str
    tug_id:str
    segments:tuple[str,...]
    window:Window
    segment_windows:tuple[Window,...]
    energy_kwh:float

class TowingManager:
    def __init__(self,tugs:Iterable[Tug],segments:Iterable[TowSegment]):
        self.tugs={t.tug_id:t for t in tugs}
        self.segments={s.segment_id:s for s in segments}
        self.out:dict[str,list[TowSegment]]={}
        for s in self.segments.values():self.out.setdefault(s.src,[]).append(s)
        self._tug_use:dict[str,list[Window]]={t:[] for t in self.tugs}
        self._segment_use:dict[str,list[tuple[Window,str]]]={}
        self.plans:dict[str,TowPlan]={}

    def _route(self,job:TowJob)->tuple[str,...]:
        heap=[(0.0,job.from_node,())];best={job.from_node:0.0}
        while heap:
            cost,node,path=heappop(heap)
            if cost!=best[node]:continue
            if node==job.to_node:return path
            for s in self.out.get(node,()):
                if job.weight_kg>s.max_tow_kg:continue
                nc=cost+s.length_m
                if nc<best.get(s.dst,float("inf")):
                    best[s.dst]=nc;heappush(heap,(nc,s.dst,path+(s.segment_id,)))
        raise RuntimeError("no tow route")

    def _energy(self,tug:Tug,job:TowJob,segments:tuple[str,...])->float:
        distance=sum(self.segments[s].length_m for s in segments)
        rolling=0.000011*job.weight_kg*distance/1000
        aux=0.4*(job.attach_min+job.detach_min)/60
        return rolling+aux

    def _group_busy(self,segment:TowSegment,window:Window)->bool:
        group=segment.conflict_group or segment.segment_id
        for sid,s in self.segments.items():
            if (s.conflict_group or s.segment_id)!=group:continue
            if any(w.overlaps(window) for w,_ in self._segment_use.get(sid,())):
                return True
        return False

    def plan(self,job:TowJob)->TowPlan:
        if job.job_id in self.plans:raise ValueError("duplicate tow job")
        route=self._route(job)
        candidates=[]
        for tug in self.tugs.values():
            if not tug.available or job.weight_kg>tug.max_tow_kg or job.gear_type not in tug.compatible_gears:continue
            energy=self._energy(tug,job,route)
            if tug.battery_kwh-energy<tug.reserve_kwh:continue
            cursor=job.earliest
            for _ in range(180):
                segment_windows=[]
                cur=cursor+timedelta(minutes=job.attach_min)
                conflict_end=None
                for sid in route:
                    seg=self.segments[sid]
                    sec=max(1,int(seg.length_m/tug.speed_m_s+0.999))
                    w=Window(cur,cur+timedelta(seconds=sec))
                    if self._group_busy(seg,w):
                        ends=[]
                        group=seg.conflict_group or seg.segment_id
                        for osid,other in self.segments.items():
                            if (other.conflict_group or other.segment_id)==group:
                                ends.extend(x.end for x,_ in self._segment_use.get(osid,()) if x.overlaps(w))
                        conflict_end=max(ends) if ends else cur+timedelta(minutes=1)
                        break
                    segment_windows.append(w);cur=w.end
                if conflict_end is not None:
                    cursor=conflict_end;continue
                full=Window(cursor,cur+timedelta(minutes=job.detach_min))
                if full.end>job.latest:break
                if any(w.overlaps(full) for w in self._tug_use[tug.tug_id]):
                    cursor=max(w.end for w in self._tug_use[tug.tug_id] if w.overlaps(full));continue
                candidates.append((full.end,tug.tug_id,TowPlan(job.job_id,tug.tug_id,route,full,tuple(segment_windows),energy)));break
        if not candidates:raise RuntimeError("no feasible tow")
        candidates.sort(key=lambda x:(x[0],x[1]))
        return candidates[0][2]

    def commit(self,plan:TowPlan)->Decision:
        tug=self.tugs[plan.tug_id]
        if any(w.overlaps(plan.window) for w in self._tug_use[plan.tug_id]):
            return Decision(False,"tug-stale","tug reservation changed")
        for sid,w in zip(plan.segments,plan.segment_windows):
            if self._group_busy(self.segments[sid],w):
                return Decision(False,"route-stale","tow route became occupied",{"segment":sid})
        if tug.battery_kwh-plan.energy_kwh<tug.reserve_kwh:
            return Decision(False,"energy","tug reserve would be violated")
        self._tug_use[plan.tug_id].append(plan.window)
        for sid,w in zip(plan.segments,plan.segment_windows):
            self._segment_use.setdefault(sid,[]).append((w,plan.job_id))
        self.tugs[plan.tug_id]=replace(tug,battery_kwh=tug.battery_kwh-plan.energy_kwh)
        self.plans[plan.job_id]=plan
        return Decision(True,"ok","tow committed",{"energy_kwh":plan.energy_kwh})

    def cancel(self,job_id:str)->bool:
        p=self.plans.pop(job_id,None)
        if p is None:return False
        tug=self.tugs[p.tug_id]
        self.tugs[p.tug_id]=replace(tug,battery_kwh=min(tug.battery_kwh+p.energy_kwh,1000000))
        self._tug_use[p.tug_id]=[w for w in self._tug_use[p.tug_id] if w!=p.window]
        for sid in p.segments:
            self._segment_use[sid]=[(w,j) for w,j in self._segment_use.get(sid,()) if j!=job_id]
        return True
