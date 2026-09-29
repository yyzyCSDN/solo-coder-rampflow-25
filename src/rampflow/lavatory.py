from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class LavTruck:
    truck_id:str
    waste_capacity_l:float
    waste_l:float
    rinse_l:float
    pump_lpm:float
    chemical_l:float
    minimum_chemical_l:float

@dataclass(frozen=True)
class LavRequest:
    request_id:str
    flight_id:str
    expected_waste_l:float
    rinse_needed_l:float
    earliest:datetime
    latest:datetime
    chemical_dose_l:float
    def __post_init__(self):
        object.__setattr__(self,"earliest",utc(self.earliest));object.__setattr__(self,"latest",utc(self.latest))

@dataclass(frozen=True)
class LavService:
    request_id:str
    truck_id:str
    window:Window
    waste_l:float
    rinse_l:float
    chemical_l:float

class LavatoryManager:
    def __init__(self,trucks:Iterable[LavTruck]):
        self.trucks={t.truck_id:t for t in trucks}
        self._use:dict[str,list[Window]]={t:[] for t in self.trucks}
        self.services:dict[str,LavService]={}

    def plan(self,req:LavRequest)->LavService:
        options=[]
        for t in self.trucks.values():
            if t.waste_l+req.expected_waste_l>t.waste_capacity_l:continue
            if t.rinse_l<req.rinse_needed_l:continue
            if t.chemical_l-req.chemical_dose_l<t.minimum_chemical_l:continue
            duration=max(5,int((req.expected_waste_l+req.rinse_needed_l)/t.pump_lpm+0.999)+3)
            cur=req.earliest
            for _ in range(120):
                w=Window(cur,cur+timedelta(minutes=duration))
                overlap=[x for x in self._use[t.truck_id] if x.overlaps(w)]
                if not overlap:break
                cur=max(x.end for x in overlap)
            if w.end<=req.latest:options.append((w.end,t.waste_capacity_l-t.waste_l,t.truck_id,LavService(req.request_id,t.truck_id,w,req.expected_waste_l,req.rinse_needed_l,req.chemical_dose_l)))
        if not options:raise RuntimeError("no lavatory service")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3]

    def commit(self,s:LavService)->Decision:
        if s.request_id in self.services:return Decision(False,"duplicate","lavatory request already served")
        t=self.trucks[s.truck_id]
        if any(w.overlaps(s.window) for w in self._use[s.truck_id]):return Decision(False,"truck-stale","lavatory truck unavailable")
        if t.waste_l+s.waste_l>t.waste_capacity_l:return Decision(False,"waste-capacity","lavatory waste tank capacity changed")
        if t.rinse_l<s.rinse_l:return Decision(False,"rinse","insufficient rinse water")
        if t.chemical_l-s.chemical_l<t.minimum_chemical_l:return Decision(False,"chemical","chemical reserve would be violated")
        self.trucks[s.truck_id]=replace(t,waste_l=t.waste_l+s.waste_l,rinse_l=t.rinse_l-s.rinse_l,chemical_l=t.chemical_l-s.chemical_l)
        self._use[s.truck_id].append(s.window);self.services[s.request_id]=s
        return Decision(True,"ok","lavatory service committed")

    def dump(self,truck_id:str,at:datetime)->LavTruck:
        when=utc(at)
        if any(w.contains(when,end_inclusive=True) for w in self._use[truck_id]):raise RuntimeError("truck in service")
        t=self.trucks[truck_id];self.trucks[truck_id]=replace(t,waste_l=0)
        return self.trucks[truck_id]
