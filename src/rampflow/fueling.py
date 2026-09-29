from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime,timedelta
from decimal import Decimal
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class FuelTruck:
    truck_id:str
    fuel_grade:str
    capacity_l:float
    quantity_l:float
    pump_lpm:float
    meter_factor:float=1.0
    def __post_init__(self):
        if self.capacity_l<=0 or not 0<=self.quantity_l<=self.capacity_l:
            raise ValueError("invalid truck inventory")
        if self.pump_lpm<=0 or self.meter_factor<=0:
            raise ValueError("invalid truck calibration")

@dataclass(frozen=True)
class FuelOrder:
    order_id:str
    flight_id:str
    fuel_grade:str
    requested_kg:float
    density_15c_kg_l:float
    observed_temp_c:float
    max_uplift_l:float|None=None
    def density_at_temp(self)->float:
        expansion=0.00095
        return self.density_15c_kg_l/(1+expansion*(self.observed_temp_c-15.0))
    def required_liters(self)->float:
        liters=self.requested_kg/self.density_at_temp()
        if self.max_uplift_l is not None and liters>self.max_uplift_l+1e-9:
            raise ValueError("uplift volume exceeds aircraft limit")
        return liters

@dataclass(frozen=True)
class FuelSession:
    order_id:str
    flight_id:str
    truck_id:str
    stand_id:str
    window:Window
    delivered_l:float
    delivered_kg:float
    meter_start:float
    meter_end:float
    passengers_onboard:bool
    bonding_verified:bool

class FuelingManager:
    def __init__(self,trucks:Iterable[FuelTruck]):
        self.trucks={t.truck_id:t for t in trucks}
        self.sessions:dict[str,FuelSession]={}
        self._truck_use:dict[str,list[Window]]={t:[] for t in self.trucks}
        self._stand_use:dict[str,list[FuelSession]]={}

    def _available(self,truck_id:str,window:Window)->bool:
        return not any(x.overlaps(window) for x in self._truck_use[truck_id])

    def _stand_safe(self,stand_id:str,window:Window,passengers_onboard:bool,escape_route_clear:bool,fire_cover:bool)->Decision:
        concurrent=[s for s in self._stand_use.get(stand_id,()) if s.window.overlaps(window)]
        if concurrent:
            return Decision(False,"fuel-zone-busy","another fueling operation overlaps")
        if passengers_onboard and not escape_route_clear:
            return Decision(False,"escape-route","passenger fueling requires clear egress")
        if passengers_onboard and not fire_cover:
            return Decision(False,"fire-cover","passenger fueling requires fire cover")
        return Decision(True,"ok","fuel zone safe")

    def choose_truck(self,order:FuelOrder,window:Window)->FuelTruck:
        needed=order.required_liters()
        options=[]
        for truck in self.trucks.values():
            if truck.fuel_grade!=order.fuel_grade or not self._available(truck.truck_id,window):
                continue
            calibrated_available=truck.quantity_l*truck.meter_factor
            if calibrated_available+1e-6<needed:
                continue
            options.append((calibrated_available-needed,-truck.pump_lpm,truck.truck_id,truck))
        if not options:
            raise RuntimeError("no truck can satisfy uplift")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3]

    def schedule(self,order:FuelOrder,stand_id:str,earliest:datetime,passengers_onboard:bool=False,escape_route_clear:bool=True,fire_cover:bool=True,bonding_verified:bool=True)->FuelSession:
        if order.order_id in self.sessions:
            raise ValueError("duplicate fuel order")
        if not bonding_verified:
            raise ValueError("bonding not verified")
        needed=order.required_liters()
        start=utc(earliest)
        probe_duration=max(5,int(needed/1000)+5)
        probe=Window(start,start+timedelta(minutes=probe_duration))
        candidates=[]
        for truck in self.trucks.values():
            if truck.fuel_grade!=order.fuel_grade:
                continue
            calibrated=truck.quantity_l*truck.meter_factor
            if calibrated+1e-6<needed:
                continue
            duration=max(5,int(needed/truck.pump_lpm+0.999)+3)
            cursor=start
            for use in self._truck_use[truck.truck_id]:
                candidate=Window(cursor,cursor+timedelta(minutes=duration))
                if candidate.overlaps(use):
                    cursor=max(cursor,use.end)
            candidate=Window(cursor,cursor+timedelta(minutes=duration))
            safe=self._stand_safe(stand_id,candidate,passengers_onboard,escape_route_clear,fire_cover)
            if safe.accepted:
                candidates.append((candidate.end,candidate.start,truck.truck_id,truck,candidate))
        if not candidates:
            raise RuntimeError("no safe fueling slot")
        candidates.sort(key=lambda x:(x[0],x[1],x[2]))
        _,_,_,truck,window=candidates[0]
        physical_draw=needed/truck.meter_factor
        if physical_draw>truck.quantity_l+1e-9:
            raise RuntimeError("meter calibration leaves insufficient physical stock")
        meter_start=(truck.capacity_l-truck.quantity_l)*truck.meter_factor
        meter_end=meter_start+needed
        delivered_kg=needed*order.density_at_temp()
        updated=replace(truck,quantity_l=truck.quantity_l-physical_draw)
        self.trucks[truck.truck_id]=updated
        session=FuelSession(order.order_id,order.flight_id,truck.truck_id,stand_id,window,needed,delivered_kg,meter_start,meter_end,passengers_onboard,bonding_verified)
        self.sessions[order.order_id]=session
        self._truck_use[truck.truck_id].append(window)
        self._truck_use[truck.truck_id].sort(key=lambda w:w.start)
        self._stand_use.setdefault(stand_id,[]).append(session)
        return session

    def reconcile(self,order_id:str,actual_meter_end:float,tolerance_l:float=5.0)->Decision:
        s=self.sessions[order_id]
        delta=actual_meter_end-s.meter_end
        if abs(delta)<=tolerance_l:
            return Decision(True,"balanced","meter reconciliation within tolerance",{"variance_l":delta})
        return Decision(False,"meter-variance","meter reconciliation exceeds tolerance",{"variance_l":delta})

    def rollback(self,order_id:str)->bool:
        s=self.sessions.pop(order_id,None)
        if s is None:return False
        truck=self.trucks[s.truck_id]
        physical=s.delivered_l/truck.meter_factor
        self.trucks[s.truck_id]=replace(truck,quantity_l=min(truck.capacity_l,truck.quantity_l+physical))
        self._truck_use[s.truck_id]=[w for w in self._truck_use[s.truck_id] if w!=s.window]
        self._stand_use[s.stand_id]=[x for x in self._stand_use.get(s.stand_id,()) if x.order_id!=order_id]
        return True
