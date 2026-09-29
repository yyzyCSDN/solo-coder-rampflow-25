from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class CateringTruck:
    truck_id:str
    capacity_units:int
    chilled_capacity_units:int
    lift_min_height_m:float
    lift_max_height_m:float
    sterile_certified:bool
    depot:str

@dataclass(frozen=True)
class CateringLoad:
    load_id:str
    flight_id:str
    standard_units:int
    chilled_units:int
    allergen_groups:frozenset[str]
    sealed_at:datetime
    seal_id:str
    max_out_of_chill_min:int
    service_deadline:datetime
    door_height_m:float
    sterile_required:bool=False
    def __post_init__(self):
        object.__setattr__(self,"sealed_at",utc(self.sealed_at))
        object.__setattr__(self,"service_deadline",utc(self.service_deadline))
        if self.standard_units<0 or self.chilled_units<0 or self.chilled_units>self.standard_units:
            raise ValueError("invalid catering quantities")

@dataclass(frozen=True)
class CateringRun:
    load_id:str
    truck_id:str
    pickup:datetime
    service:Window
    returned_to_chill:datetime|None
    seal_id:str
    temperature_chain_min:int

class CateringManager:
    def __init__(self,trucks:Iterable[CateringTruck],travel_minutes:Mapping[tuple[str,str],int]):
        self.trucks={t.truck_id:t for t in trucks}
        self.travel=dict(travel_minutes)
        self._use:dict[str,list[Window]]={t:[] for t in self.trucks}
        self.runs:dict[str,CateringRun]={}
        self.revoked_seals:set[str]=set()

    def revoke_seal(self,seal_id:str)->None:
        self.revoked_seals.add(seal_id)

    def _travel(self,src:str,dst:str)->int:
        if src==dst:return 0
        if (src,dst) in self.travel:return self.travel[(src,dst)]
        if (dst,src) in self.travel:return self.travel[(dst,src)]
        raise KeyError(f"no travel time {src}->{dst}")

    def _compatible(self,truck:CateringTruck,load:CateringLoad)->bool:
        if load.standard_units>truck.capacity_units or load.chilled_units>truck.chilled_capacity_units:return False
        if not truck.lift_min_height_m<=load.door_height_m<=truck.lift_max_height_m:return False
        if load.sterile_required and not truck.sterile_certified:return False
        return True

    def plan(self,load:CateringLoad,stand:str,service_minutes:int=12)->CateringRun:
        if load.load_id in self.runs:raise ValueError("load already dispatched")
        if load.seal_id in self.revoked_seals:raise RuntimeError("seal revoked")
        options=[]
        for truck in self.trucks.values():
            if not self._compatible(truck,load):continue
            travel=self._travel(truck.depot,stand)
            pickup=max(load.sealed_at,load.service_deadline-timedelta(minutes=service_minutes+travel))
            service_start=pickup+timedelta(minutes=travel)
            service=Window(service_start,service_start+timedelta(minutes=service_minutes))
            if service.end>load.service_deadline:continue
            full=Window(pickup,service.end+timedelta(minutes=travel))
            overlaps=[w for w in self._use[truck.truck_id] if w.overlaps(full)]
            if overlaps:
                pickup=max(w.end for w in overlaps)
                service_start=pickup+timedelta(minutes=travel)
                service=Window(service_start,service_start+timedelta(minutes=service_minutes))
                full=Window(pickup,service.end+timedelta(minutes=travel))
                if service.end>load.service_deadline or any(w.overlaps(full) for w in self._use[truck.truck_id]):continue
            chain=int((service.end-pickup).total_seconds()//60)
            if load.chilled_units and chain>load.max_out_of_chill_min:continue
            options.append((service.end,chain,truck.truck_id,CateringRun(load.load_id,truck.truck_id,pickup,service,None,load.seal_id,chain),full))
        if not options:raise RuntimeError("no catering plan")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3]

    def commit(self,load:CateringLoad,stand:str,run:CateringRun)->Decision:
        if run.load_id!=load.load_id:raise ValueError("run mismatch")
        if run.seal_id!=load.seal_id or run.seal_id in self.revoked_seals:
            return Decision(False,"seal","catering seal invalid")
        truck=self.trucks[run.truck_id]
        travel=self._travel(truck.depot,stand)
        full=Window(run.pickup,run.service.end+timedelta(minutes=travel))
        if any(w.overlaps(full) for w in self._use[run.truck_id]):
            return Decision(False,"truck-stale","catering truck became unavailable")
        chain=int((run.service.end-run.pickup).total_seconds()//60)
        if load.chilled_units and chain>load.max_out_of_chill_min:
            return Decision(False,"cold-chain","chilled load exceeded exposure")
        if run.service.end>load.service_deadline:
            return Decision(False,"deadline","catering misses service deadline")
        self._use[run.truck_id].append(full)
        self.runs[load.load_id]=run
        return Decision(True,"ok","catering committed",{"exposure_min":chain})

    def return_unserved(self,load:CateringLoad,at:datetime)->Decision:
        run=self.runs.get(load.load_id)
        if run is None:return Decision(False,"unknown","load not dispatched")
        returned=utc(at)
        if returned<run.service.start:return Decision(False,"time","return before service")
        exposure=int((returned-run.pickup).total_seconds()//60)
        if load.chilled_units and exposure>load.max_out_of_chill_min:
            self.revoked_seals.add(load.seal_id)
            return Decision(False,"discard","cold chain exceeded; seal revoked",{"exposure_min":exposure})
        self.runs[load.load_id]=replace(run,returned_to_chill=returned,temperature_chain_min=exposure)
        return Decision(True,"returned","load returned to chill",{"exposure_min":exposure})
