from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc

@dataclass(frozen=True)
class SlotRequest:
    flight_id:str
    target:datetime
    tolerance_before_min:int
    tolerance_after_min:int
    priority:int=0
    required_runway:str|None=None
    wake_class:str="M"
    def __post_init__(self):
        object.__setattr__(self,"target",utc(self.target))
        if min(self.tolerance_before_min,self.tolerance_after_min)<0:raise ValueError("negative tolerance")

@dataclass(frozen=True)
class SlotAllocation:
    flight_id:str
    runway:str
    time:datetime
    request_target:datetime
    generation:int
    priority:int

@dataclass(frozen=True)
class CapacityBand:
    window:Window
    max_movements:int
    runway:str|None=None

class SlotCoordinator:
    def __init__(self,runways:Iterable[str],base_separation_seconds:int=90):
        self.runways=tuple(sorted(set(runways)))
        if not self.runways:raise ValueError("runway required")
        self.base_separation_seconds=base_separation_seconds
        self._allocations:dict[str,SlotAllocation]={};self._bands:list[CapacityBand]=[];self._generation=0

    def set_capacity(self,bands:Iterable[CapacityBand])->None:
        self._bands=list(bands);self._generation+=1

    def _separation(self,leader:str,follower:str)->int:
        extra={('H','L'):90,('H','M'):45,('M','L'):30}
        return self.base_separation_seconds+extra.get((leader,follower),0)

    def _capacity_ok(self,runway:str,t:datetime)->bool:
        for band in self._bands:
            if band.runway not in (None,runway):continue
            if not band.window.contains(t):continue
            count=sum(1 for a in self._allocations.values() if (band.runway is None or a.runway==runway) and band.window.contains(a.time))
            if count>=band.max_movements:return False
        return True

    def _safe(self,runway:str,t:datetime,wake:str,ignore_flight:str|None=None)->bool:
        if not self._capacity_ok(runway,t):return False
        for fid,a in self._allocations.items():
            if fid==ignore_flight or a.runway!=runway:continue
            if a.time<=t:
                sep=self._separation(getattr(a,'wake_class','M') if hasattr(a,'wake_class') else 'M',wake)
            else:
                sep=self._separation(wake,getattr(a,'wake_class','M') if hasattr(a,'wake_class') else 'M')
            if abs((a.time-t).total_seconds())<sep:return False
        return True

    def allocate(self,request:SlotRequest)->SlotAllocation:
        earliest=request.target-timedelta(minutes=request.tolerance_before_min)
        latest=request.target+timedelta(minutes=request.tolerance_after_min)
        candidates=[]
        runways=(request.required_runway,) if request.required_runway else self.runways
        for runway in runways:
            if runway not in self.runways:continue
            t=earliest
            while t<=latest:
                if self._safe(runway,t,request.wake_class):
                    deviation=abs((t-request.target).total_seconds())
                    candidates.append((deviation,t,runway))
                t+=timedelta(seconds=30)
        if not candidates:raise RuntimeError("no feasible runway slot")
        _,t,runway=min(candidates,key=lambda x:(x[0],x[1],x[2]))
        return SlotAllocation(request.flight_id,runway,t,request.target,self._generation,request.priority)

    def commit(self,allocation:SlotAllocation,wake_class:str='M')->None:
        existing=self._allocations.get(allocation.flight_id)
        if existing and existing!=allocation:raise RuntimeError("flight already allocated")
        if allocation.generation!=self._generation and not self._safe(allocation.runway,allocation.time,wake_class,allocation.flight_id):
            raise RuntimeError("slot allocation stale")
        if not self._safe(allocation.runway,allocation.time,wake_class,allocation.flight_id):raise RuntimeError("slot conflict")
        self._allocations[allocation.flight_id]=allocation;self._generation+=1

    def swap(self,first:str,second:str)->tuple[SlotAllocation,SlotAllocation]:
        a=self._allocations[first];b=self._allocations[second]
        na=replace(a,runway=b.runway,time=b.time,generation=self._generation)
        nb=replace(b,runway=a.runway,time=a.time,generation=self._generation)
        temp=dict(self._allocations);del self._allocations[first];del self._allocations[second]
        try:
            if not self._safe(na.runway,na.time,'M') or not self._safe(nb.runway,nb.time,'M'):raise RuntimeError("swap violates separation")
            self._allocations[first]=na;self._allocations[second]=nb;self._generation+=1
            return na,nb
        except Exception:
            self._allocations=temp;raise

    def allocations(self)->tuple[SlotAllocation,...]:return tuple(sorted(self._allocations.values(),key=lambda a:(a.time,a.flight_id)))
