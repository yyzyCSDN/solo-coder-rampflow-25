from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class WaterTruck:
    truck_id:str
    capacity_l:float
    quantity_l:float
    flow_lpm:float
    sanitized_at:datetime
    sanitation_valid_hours:int=24
    def __post_init__(self):
        object.__setattr__(self,"sanitized_at",utc(self.sanitized_at))
        if not 0<=self.quantity_l<=self.capacity_l:raise ValueError("invalid water truck inventory")

@dataclass(frozen=True)
class WaterRequest:
    request_id:str
    flight_id:str
    liters:float
    earliest:datetime
    latest:datetime
    minimum_residual_l:float=0
    def __post_init__(self):
        object.__setattr__(self,"earliest",utc(self.earliest));object.__setattr__(self,"latest",utc(self.latest))
        if self.liters<=0 or self.latest<=self.earliest:raise ValueError("invalid water request")

@dataclass(frozen=True)
class WaterService:
    request_id:str
    truck_id:str
    window:Window
    liters:float

class PotableWaterManager:
    def __init__(self,trucks:Iterable[WaterTruck]):
        self.trucks={t.truck_id:t for t in trucks}
        self._use:dict[str,list[Window]]={t:[] for t in self.trucks}
        self.services:dict[str,WaterService]={}

    def _sanitary(self,truck:WaterTruck,at:datetime)->bool:
        return at<=truck.sanitized_at+timedelta(hours=truck.sanitation_valid_hours)

    def plan(self,req:WaterRequest)->WaterService:
        options=[]
        for truck in self.trucks.values():
            if not self._sanitary(truck,req.earliest):continue
            if truck.quantity_l-req.liters<req.minimum_residual_l:continue
            duration=max(4,int(req.liters/truck.flow_lpm+0.999)+2)
            cur=req.earliest
            for _ in range(120):
                w=Window(cur,cur+timedelta(minutes=duration))
                hit=[x for x in self._use[truck.truck_id] if x.overlaps(w)]
                if not hit:break
                cur=max(x.end for x in hit)
            if w.end<=req.latest and self._sanitary(truck,w.end):
                options.append((w.end,truck.quantity_l-req.liters,truck.truck_id,WaterService(req.request_id,truck.truck_id,w,req.liters)))
        if not options:raise RuntimeError("no potable water service")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3]

    def commit(self,service:WaterService)->Decision:
        if service.request_id in self.services:return Decision(False,"duplicate","water request already served")
        truck=self.trucks[service.truck_id]
        if not self._sanitary(truck,service.window.end):return Decision(False,"sanitation","water truck sanitation expired")
        if truck.quantity_l<service.liters:return Decision(False,"inventory","water truck quantity changed")
        if any(w.overlaps(service.window) for w in self._use[service.truck_id]):return Decision(False,"truck-stale","water truck became unavailable")
        self.trucks[service.truck_id]=replace(truck,quantity_l=truck.quantity_l-service.liters)
        self._use[service.truck_id].append(service.window);self.services[service.request_id]=service
        return Decision(True,"ok","potable water service committed")

    def refill_and_sanitize(self,truck_id:str,at:datetime,liters:float)->WaterTruck:
        t=self.trucks[truck_id];when=utc(at)
        if any(w.contains(when,end_inclusive=True) for w in self._use[truck_id]):raise RuntimeError("truck active")
        self.trucks[truck_id]=replace(t,quantity_l=min(t.capacity_l,t.quantity_l+liters),sanitized_at=when)
        return self.trucks[truck_id]
