from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class Equipment:
    equipment_id:str
    kind:str
    capabilities:frozenset[str]
    home_zone:str
    battery_pct:float=100.0
    minimum_pct:float=10.0
    travel_kph:float=15.0
    out_of_service:bool=False

@dataclass(frozen=True)
class EquipmentJob:
    job_id:str
    kind:str
    zone:str
    window:Window
    capabilities:frozenset[str]=frozenset()
    travel_distance_km:float=0.0
    energy_pct_per_km:float=1.0

@dataclass(frozen=True)
class EquipmentAssignment:
    job_id:str
    equipment_id:str
    travel:Window|None
    service:Window
    battery_after:float

class EquipmentPool:
    def __init__(self,equipment:Iterable[Equipment],zone_distance_km:Mapping[tuple[str,str],float]=None):
        self.equipment={e.equipment_id:e for e in equipment}
        self.distances=dict(zone_distance_km or {})
        self._use:dict[str,list[Window]]={e:[] for e in self.equipment}
        self._zone:dict[str,str]={e.equipment_id:e.home_zone for e in self.equipment}
        self.assignments:dict[str,EquipmentAssignment]={}

    def _distance(self,a:str,b:str)->float:
        if a==b:return 0.0
        return self.distances.get((a,b),self.distances.get((b,a),5.0))

    def assign(self,job:EquipmentJob)->EquipmentAssignment:
        options=[]
        for e in self.equipment.values():
            if e.out_of_service or e.kind!=job.kind or not job.capabilities.issubset(e.capabilities):continue
            origin=self._zone[e.equipment_id]
            distance=self._distance(origin,job.zone)
            travel_min=int(distance/max(0.1,e.travel_kph)*60+0.999)
            travel=Window(job.window.start-timedelta(minutes=travel_min),job.window.start) if travel_min else None
            occupied=job.window if travel is None else Window(travel.start,job.window.end)
            if any(w.overlaps(occupied) for w in self._use[e.equipment_id]):continue
            energy=distance*job.energy_pct_per_km+job.travel_distance_km*job.energy_pct_per_km
            after=e.battery_pct-energy
            if after<e.minimum_pct:continue
            options.append((distance,after,e.equipment_id,EquipmentAssignment(job.job_id,e.equipment_id,travel,job.window,after)))
        if not options:raise RuntimeError("no ground equipment")
        options.sort(key=lambda x:(x[0],-x[1],x[2]))
        return options[0][3]

    def commit(self,a:EquipmentAssignment,job:EquipmentJob)->Decision:
        if a.job_id in self.assignments:return Decision(False,"duplicate","equipment job already assigned")
        e=self.equipment[a.equipment_id]
        occupied=a.service if a.travel is None else Window(a.travel.start,a.service.end)
        if any(w.overlaps(occupied) for w in self._use[a.equipment_id]):return Decision(False,"equipment-stale","equipment became occupied")
        if a.battery_after<e.minimum_pct:return Decision(False,"energy","equipment reserve violated")
        self._use[a.equipment_id].append(occupied);self._zone[a.equipment_id]=job.zone
        self.equipment[a.equipment_id]=replace(e,battery_pct=a.battery_after)
        self.assignments[a.job_id]=a
        return Decision(True,"ok","ground equipment committed")

    def charge(self,equipment_id:str,window:Window,rate_pct_min:float)->Equipment:
        if any(w.overlaps(window) for w in self._use[equipment_id]):raise RuntimeError("equipment busy")
        e=self.equipment[equipment_id]
        self._use[equipment_id].append(window)
        self.equipment[equipment_id]=replace(e,battery_pct=min(100,e.battery_pct+window.minutes*rate_pct_min))
        return self.equipment[equipment_id]

    def quarantine(self,equipment_id:str)->None:
        self.equipment[equipment_id]=replace(self.equipment[equipment_id],out_of_service=True)
