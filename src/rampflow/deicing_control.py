from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc

@dataclass(frozen=True)
class FluidRule:
    fluid_type:str
    precipitation:str
    temperature_min_c:float
    temperature_max_c:float
    holdover_min:int
    holdover_max:int

@dataclass(frozen=True)
class DeicingJob:
    flight_id:str
    pad:str
    started_at:datetime
    completed_at:datetime
    fluid_type:str
    precipitation:str
    temperature_c:float
    holdover_expires:datetime
    verification_due:datetime
    version:int=1

class DeicingController:
    def __init__(self,pads:Iterable[str],rules:Iterable[FluidRule],verification_margin_min:int=5):
        self.pads=tuple(sorted(set(pads)));self.rules=tuple(rules);self.margin=verification_margin_min
        self.jobs:dict[str,DeicingJob]={};self.reservations:dict[str,list[tuple[str,Window]]]={p:[] for p in self.pads}

    def _rule(self,fluid:str,precipitation:str,temp:float)->FluidRule:
        options=[r for r in self.rules if r.fluid_type==fluid and r.precipitation==precipitation and r.temperature_min_c<=temp<=r.temperature_max_c]
        if not options:raise RuntimeError('no applicable holdover rule')
        return min(options,key=lambda r:(r.holdover_max,r.holdover_min))

    def schedule(self,flight_id:str,earliest:datetime,duration_min:int,fluid:str,precipitation:str,temp_c:float)->DeicingJob:
        earliest=utc(earliest);rule=self._rule(fluid,precipitation,temp_c)
        candidates=[]
        for pad in self.pads:
            start=earliest
            for _ in range(200):
                w=Window(start,start+timedelta(minutes=duration_min))
                clashes=[used for _,used in self.reservations[pad] if used.overlaps(w)]
                if not clashes:break
                start=max(x.end for x in clashes)
            else:continue
            candidates.append((w.end,pad,w))
        if not candidates:raise RuntimeError('no deicing capacity')
        _,pad,w=min(candidates,key=lambda x:(x[0],x[1]))
        expiry=w.end+timedelta(minutes=rule.holdover_min)
        verify=expiry-timedelta(minutes=self.margin)
        return DeicingJob(flight_id,pad,w.start,w.end,fluid,precipitation,temp_c,expiry,verify)

    def commit(self,job:DeicingJob)->None:
        if job.flight_id in self.jobs:raise RuntimeError('job already committed')
        w=Window(job.started_at,job.completed_at)
        if any(u.overlaps(w) for _,u in self.reservations[job.pad]):raise RuntimeError('deicing pad conflict')
        self.reservations[job.pad].append((job.flight_id,w));self.jobs[job.flight_id]=job

    def weather_change(self,flight_id:str,at:datetime,precipitation:str,temp_c:float)->DeicingJob:
        old=self.jobs[flight_id];at=utc(at)
        if at<old.completed_at:raise RuntimeError('weather update precedes treatment completion')
        rule=self._rule(old.fluid_type,precipitation,temp_c)
        candidate=old.completed_at+timedelta(minutes=rule.holdover_min)
        expiry=min(old.holdover_expires,candidate) if precipitation!=old.precipitation or temp_c<old.temperature_c else candidate
        updated=replace(old,precipitation=precipitation,temperature_c=temp_c,holdover_expires=expiry,verification_due=expiry-timedelta(minutes=self.margin),version=old.version+1)
        self.jobs[flight_id]=updated;return updated

    def release_status(self,flight_id:str,offblock:datetime,inspection_passed_at:datetime|None=None)->str:
        job=self.jobs[flight_id];offblock=utc(offblock)
        if offblock<=job.holdover_expires:return 'valid'
        if inspection_passed_at is not None:
            inspection_passed_at=utc(inspection_passed_at)
            if inspection_passed_at>=job.verification_due and inspection_passed_at<=offblock:return 'inspection-required-release'
        return 'expired'
