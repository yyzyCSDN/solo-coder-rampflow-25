from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from secrets import token_hex
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class GPU:
    gpu_id:str
    voltage:int
    max_kva:float
    battery_kwh:float
    reserve_kwh:float
    efficiency:float=0.90
    connector:str="400Hz"
    active:bool=True

@dataclass(frozen=True)
class GPULease:
    lease_id:str
    gpu_id:str
    flight_id:str
    stand_id:str
    window:Window
    kva:float
    token:str
    expected_kwh:float
    generation:int

class GPUManager:
    def __init__(self,units:Iterable[GPU]):
        self.units={g.gpu_id:g for g in units}
        self.leases:dict[str,GPULease]={}
        self._use:dict[str,list[GPULease]]={g:[] for g in self.units}
        self._generation=0

    def _energy(self,kva:float,window:Window,power_factor:float)->float:
        if not 0<power_factor<=1:raise ValueError("invalid power factor")
        return kva*power_factor*(window.minutes/60)

    def candidates(self,voltage:int,connector:str,kva:float,window:Window,power_factor:float=0.9)->list[GPU]:
        result=[]
        for g in self.units.values():
            if not g.active or g.voltage!=voltage or g.connector!=connector or kva>g.max_kva:continue
            if any(x.window.overlaps(window) for x in self._use[g.gpu_id]):continue
            draw=self._energy(kva,window,power_factor)/g.efficiency
            if g.battery_kwh-draw<g.reserve_kwh:continue
            result.append(g)
        return sorted(result,key=lambda x:(x.battery_kwh-x.reserve_kwh,x.gpu_id))

    def lease(self,flight_id:str,stand_id:str,voltage:int,connector:str,kva:float,window:Window,power_factor:float=0.9)->GPULease:
        options=self.candidates(voltage,connector,kva,window,power_factor)
        if not options:raise RuntimeError("no compatible GPU")
        g=options[0]
        draw=self._energy(kva,window,power_factor)/g.efficiency
        self._generation+=1
        lease=GPULease(f"gpu-{self._generation}",g.gpu_id,flight_id,stand_id,window,kva,token_hex(12),draw,self._generation)
        self._use[g.gpu_id].append(lease)
        self.leases[lease.lease_id]=lease
        self.units[g.gpu_id]=replace(g,battery_kwh=g.battery_kwh-draw)
        return lease

    def extend(self,lease_id:str,token:str,new_end:datetime)->Decision:
        lease=self.leases[lease_id]
        if lease.token!=token:return Decision(False,"token","lease token mismatch")
        end=utc(new_end)
        if end<=lease.window.end:return Decision(False,"window","extension must move end forward")
        g=self.units[lease.gpu_id]
        added=Window(lease.window.end,end)
        for other in self._use[lease.gpu_id]:
            if other.lease_id!=lease_id and other.window.overlaps(added):
                return Decision(False,"occupied","GPU has a subsequent lease")
        extra=lease.kva*0.9*(added.minutes/60)/g.efficiency
        if g.battery_kwh-extra<g.reserve_kwh:return Decision(False,"energy","GPU reserve would be violated")
        self._generation+=1
        updated=replace(lease,window=Window(lease.window.start,end),expected_kwh=lease.expected_kwh+extra,generation=self._generation)
        self.leases[lease_id]=updated
        self._use[lease.gpu_id]=[updated if x.lease_id==lease_id else x for x in self._use[lease.gpu_id]]
        self.units[lease.gpu_id]=replace(g,battery_kwh=g.battery_kwh-extra)
        return Decision(True,"ok","GPU lease extended",{"generation":self._generation})

    def handoff(self,lease_id:str,token:str,to_flight:str,at:datetime)->GPULease:
        lease=self.leases[lease_id]
        if token!=lease.token:raise PermissionError("lease token mismatch")
        t=utc(at)
        if not lease.window.contains(t) and t!=lease.window.end:raise ValueError("handoff outside lease")
        if t==lease.window.start:raise ValueError("zero-length source lease")
        source=replace(lease,window=Window(lease.window.start,t))
        self.leases[lease_id]=source
        new_id=f"gpu-{self._generation+1}"
        self._generation+=1
        target=GPULease(new_id,lease.gpu_id,to_flight,lease.stand_id,Window(t,lease.window.end),lease.kva,token_hex(12),lease.expected_kwh*(lease.window.end-t)/(lease.window.end-lease.window.start),self._generation) if t<lease.window.end else None
        self._use[lease.gpu_id]=[source if x.lease_id==lease_id else x for x in self._use[lease.gpu_id]]
        if target is None:raise ValueError("handoff at lease end has no remaining service")
        self._use[lease.gpu_id].append(target);self.leases[new_id]=target
        return target

    def release(self,lease_id:str,token:str,actual_end:datetime|None=None)->Decision:
        lease=self.leases.get(lease_id)
        if lease is None:return Decision(False,"unknown","lease not found")
        if token!=lease.token:return Decision(False,"token","lease token mismatch")
        end=utc(actual_end) if actual_end else lease.window.end
        if end<lease.window.start or end>lease.window.end:return Decision(False,"time","actual end outside lease")
        if end<lease.window.end:
            g=self.units[lease.gpu_id]
            unused=lease.expected_kwh*((lease.window.end-end)/(lease.window.end-lease.window.start))
            self.units[lease.gpu_id]=replace(g,battery_kwh=g.battery_kwh+unused)
        self._use[lease.gpu_id]=[x for x in self._use[lease.gpu_id] if x.lease_id!=lease_id]
        self.leases.pop(lease_id,None)
        return Decision(True,"released","GPU lease released")
