from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window
from .models import Decision

@dataclass(frozen=True)
class PCAUnit:
    unit_id:str
    max_flow_kg_s:float
    nominal_kw:float
    min_supply_c:float
    max_supply_c:float
    compatible_stands:frozenset[str]
    derate_start_c:float=32.0
    derate_per_c:float=0.025

@dataclass(frozen=True)
class CabinCondition:
    cabin_temp_c:float
    target_temp_c:float
    passengers:int
    doors_open:int
    solar_kw:float
    ambient_c:float
    humidity_pct:float

@dataclass(frozen=True)
class PCASession:
    flight_id:str
    unit_id:str
    stand_id:str
    window:Window
    required_flow_kg_s:float
    available_flow_kg_s:float
    supply_temp_c:float
    estimated_kw:float
    coupled_gpu_lease:str|None

class PCAManager:
    def __init__(self,units:Iterable[PCAUnit]):
        self.units={u.unit_id:u for u in units}
        self._use:dict[str,list[PCASession]]={u:[] for u in self.units}
        self.sessions:dict[str,PCASession]={}

    def _heat_kw(self,c:CabinCondition)->float:
        passenger=0.115*c.passengers
        door=4.5*c.doors_open*max(0.0,c.ambient_c-c.target_temp_c)/10
        sensible=2.2*max(0.0,c.cabin_temp_c-c.target_temp_c)
        humidity=max(0.0,c.humidity_pct-55)*0.12
        return max(0.0,passenger+door+sensible+c.solar_kw+humidity)

    def _required_flow(self,c:CabinCondition,supply_c:float)->float:
        delta=max(2.0,c.target_temp_c-supply_c)
        cp=1.005
        return self._heat_kw(c)/(cp*delta)

    def _available_flow(self,u:PCAUnit,ambient_c:float)->float:
        if ambient_c<=u.derate_start_c:return u.max_flow_kg_s
        factor=max(0.45,1-u.derate_per_c*(ambient_c-u.derate_start_c))
        return u.max_flow_kg_s*factor

    def _power(self,u:PCAUnit,flow:float,condition:CabinCondition)->float:
        flow_ratio=min(1.5,flow/u.max_flow_kg_s)
        lift=max(1.0,condition.ambient_c-condition.target_temp_c)
        humidity_factor=1+max(0.0,condition.humidity_pct-60)/200
        return u.nominal_kw*(0.35+0.65*flow_ratio**2)*(1+lift/80)*humidity_factor

    def plan(self,flight_id:str,stand_id:str,window:Window,condition:CabinCondition,gpu_capacity_kw:float|None=None,gpu_lease_id:str|None=None)->PCASession:
        if flight_id in self.sessions:raise ValueError("PCA already planned")
        options=[]
        for u in self.units.values():
            if u.compatible_stands and stand_id not in u.compatible_stands:continue
            if any(x.window.overlaps(window) for x in self._use[u.unit_id]):continue
            supply=max(u.min_supply_c,min(u.max_supply_c,condition.target_temp_c-8))
            required=self._required_flow(condition,supply)
            available=self._available_flow(u,condition.ambient_c)
            if required>available+1e-9:continue
            power=self._power(u,required,condition)
            if gpu_capacity_kw is not None and power>gpu_capacity_kw+1e-9:continue
            options.append((power,available-required,u.unit_id,PCASession(flight_id,u.unit_id,stand_id,window,required,available,supply,power,gpu_lease_id)))
        if not options:raise RuntimeError("no PCA unit can meet cabin load")
        options.sort(key=lambda x:(x[0],-x[1],x[2]))
        return options[0][3]

    def commit(self,session:PCASession,current_gpu_capacity_kw:float|None=None,current_gpu_lease_id:str|None=None)->Decision:
        if any(x.window.overlaps(session.window) for x in self._use[session.unit_id]):
            return Decision(False,"unit-stale","PCA unit became unavailable")
        if session.coupled_gpu_lease is not None:
            if current_gpu_lease_id!=session.coupled_gpu_lease:
                return Decision(False,"gpu-generation","coupled GPU lease changed")
            if current_gpu_capacity_kw is None or current_gpu_capacity_kw<session.estimated_kw:
                return Decision(False,"gpu-capacity","coupled GPU lacks PCA capacity")
        self._use[session.unit_id].append(session)
        self.sessions[session.flight_id]=session
        return Decision(True,"ok","PCA session committed",{"kw":session.estimated_kw})

    def reassess(self,flight_id:str,condition:CabinCondition)->Decision:
        s=self.sessions[flight_id];u=self.units[s.unit_id]
        required=self._required_flow(condition,s.supply_temp_c)
        available=self._available_flow(u,condition.ambient_c)
        if required>available:
            return Decision(False,"thermal-overload","ambient conditions exceed PCA capacity",{"required":required,"available":available})
        power=self._power(u,required,condition)
        return Decision(True,"stable","PCA remains adequate",{"required":required,"available":available,"kw":power})
