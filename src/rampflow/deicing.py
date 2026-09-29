from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class HoldoverBand:
    precip: str
    fluid_type: str
    min_temp_c: float
    max_temp_c: float
    min_minutes: int
    max_minutes: int
    concentration: int = 100
    def applies(self,precip:str,fluid_type:str,temp_c:float,concentration:int)->bool:
        return self.precip==precip and self.fluid_type==fluid_type and self.min_temp_c<=temp_c<self.max_temp_c and concentration>=self.concentration

@dataclass(frozen=True)
class Treatment:
    flight_id: str
    pad_id: str
    start: datetime
    end: datetime
    fluid_type: str
    concentration: int
    precip: str
    temp_c: float
    holdover_min: int
    holdover_max: int
    inspection_interval_min: int
    sequence: int
    @property
    def conservative_expiry(self)->datetime:
        return self.end+timedelta(minutes=self.holdover_min)
    @property
    def optimistic_expiry(self)->datetime:
        return self.end+timedelta(minutes=self.holdover_max)

@dataclass(frozen=True)
class Pad:
    pad_id:str
    bays:int
    supported_fluids:frozenset[str]
    max_wingspan_m:float
    throughput_lpm:float

class DeicingManager:
    def __init__(self,pads:Iterable[Pad],bands:Iterable[HoldoverBand]):
        self.pads={p.pad_id:p for p in pads}
        self.bands=tuple(bands)
        self.treatments:dict[str,Treatment]={}
        self._pad_use:dict[str,list[Window]]={p:[] for p in self.pads}
        self._sequence=0

    def _holdover(self,precip:str,fluid_type:str,temp_c:float,concentration:int)->tuple[int,int]:
        candidates=[b for b in self.bands if b.applies(precip,fluid_type,temp_c,concentration)]
        if not candidates:
            raise ValueError("no holdover guidance")
        candidates.sort(key=lambda b:(b.concentration,b.min_temp_c),reverse=True)
        chosen=candidates[0]
        return chosen.min_minutes,chosen.max_minutes

    def _inspection_interval(self,holdover_min:int,precip:str)->int:
        factor={"freezing-rain":Decimal("0.25"),"heavy-snow":Decimal("0.33"),"snow":Decimal("0.50"),"frost":Decimal("0.75")}.get(precip,Decimal("0.50"))
        return max(2,int((Decimal(holdover_min)*factor).quantize(Decimal("1"),rounding=ROUND_FLOOR)))

    def estimate_minutes(self,pad_id:str,wingspan_m:float,fluid_liters:float)->int:
        pad=self.pads[pad_id]
        if wingspan_m>pad.max_wingspan_m:
            raise ValueError("aircraft too large for pad")
        base=fluid_liters/max(1.0,pad.throughput_lpm)
        setup=4.0 + max(0.0,wingspan_m-36.0)/12.0
        return max(5,int(base+setup+0.999))

    def find_slot(self,pad_id:str,earliest:datetime,duration_min:int)->Window:
        pad=self.pads[pad_id]
        start=utc(earliest)
        duration=timedelta(minutes=duration_min)
        events=[]
        for use in self._pad_use[pad_id]:
            events.append((use.start,1));events.append((use.end,-1))
        events.sort(key=lambda x:(x[0],x[1]))
        cursor=start
        for _ in range(300):
            candidate=Window(cursor,cursor+duration)
            overlaps=[u for u in self._pad_use[pad_id] if u.overlaps(candidate)]
            if len(overlaps)<pad.bays:
                points=[candidate.start,candidate.end]
                for u in overlaps: points.extend([u.start,u.end])
                ok=True
                for a,b in zip(sorted(set(points)),sorted(set(points))[1:]):
                    mid=a+(b-a)/2
                    active=sum(1 for u in self._pad_use[pad_id] if u.contains(mid))
                    if candidate.contains(mid):active+=1
                    if active>pad.bays:
                        ok=False;break
                if ok:return candidate
            future=[u.end for u in overlaps if u.end>cursor]
            cursor=min(future) if future else cursor+timedelta(minutes=1)
        raise RuntimeError("no deicing slot")

    def schedule(self,flight_id:str,pad_id:str,earliest:datetime,wingspan_m:float,fluid_type:str,concentration:int,precip:str,temp_c:float,fluid_liters:float)->Treatment:
        if flight_id in self.treatments:
            raise ValueError("flight already treated")
        pad=self.pads[pad_id]
        if fluid_type not in pad.supported_fluids:
            raise ValueError("fluid unsupported by pad")
        hmin,hmax=self._holdover(precip,fluid_type,temp_c,concentration)
        duration=self.estimate_minutes(pad_id,wingspan_m,fluid_liters)
        slot=self.find_slot(pad_id,earliest,duration)
        self._sequence += 1
        treatment=Treatment(flight_id,pad_id,slot.start,slot.end,fluid_type,concentration,precip,temp_c,hmin,hmax,self._inspection_interval(hmin,precip),self._sequence)
        self.treatments[flight_id]=treatment
        self._pad_use[pad_id].append(slot)
        self._pad_use[pad_id].sort(key=lambda w:w.start)
        return treatment

    def departure_status(self,flight_id:str,departure:datetime,last_inspection:datetime|None=None)->Decision:
        if flight_id not in self.treatments:
            return Decision(False,"untreated","no deicing treatment")
        t=self.treatments[flight_id]
        dep=utc(departure)
        if dep <= t.conservative_expiry:
            return Decision(True,"within-holdover","departure within conservative holdover",{"expiry":t.conservative_expiry})
        if dep > t.optimistic_expiry:
            return Decision(False,"expired","holdover guidance expired",{"expiry":t.optimistic_expiry})
        if last_inspection is None:
            return Decision(False,"inspection-required","departure is in extended holdover interval")
        inspected=utc(last_inspection)
        if inspected < t.end or inspected > dep:
            return Decision(False,"invalid-inspection","inspection timestamp outside valid interval")
        if dep-inspected > timedelta(minutes=t.inspection_interval_min):
            return Decision(False,"inspection-stale","post-treatment inspection is too old")
        return Decision(True,"inspected","extended holdover accepted after inspection",{"inspection":inspected})

    def cancel(self,flight_id:str)->bool:
        t=self.treatments.pop(flight_id,None)
        if t is None:return False
        window=Window(t.start,t.end)
        self._pad_use[t.pad_id]=[x for x in self._pad_use[t.pad_id] if x!=window]
        return True
