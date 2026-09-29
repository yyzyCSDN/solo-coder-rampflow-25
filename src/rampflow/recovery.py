from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class RecoveryFlight:
    flight_id:str
    aircraft_family:str
    origin:str
    destination:str
    departure:datetime
    arrival:datetime
    seats:int
    assigned_aircraft:str
    crew_base:str
    def __post_init__(self):
        object.__setattr__(self,"departure",utc(self.departure));object.__setattr__(self,"arrival",utc(self.arrival))

@dataclass(frozen=True)
class RecoveryOption:
    option_id:str
    action:str
    flight_id:str
    replacement_flight:str|None
    delay_min:int
    passenger_spill:int
    cost:float
    notes:tuple[str,...]=()

@dataclass(frozen=True)
class Disruption:
    flight_id:str
    at:datetime
    kind:str
    duration_min:int
    affected_resource:str=""
    def __post_init__(self):
        object.__setattr__(self,"at",utc(self.at))

class RecoveryEngine:
    def __init__(self,delay_cost_per_min:float=35.0,spill_cost:float=420.0,cancel_cost:float=12000.0):
        self.delay_cost_per_min=delay_cost_per_min
        self.spill_cost=spill_cost
        self.cancel_cost=cancel_cost

    def options(self,disruption:Disruption,flight:RecoveryFlight,candidates:Iterable[RecoveryFlight],passengers:int)->tuple[RecoveryOption,...]:
        out=[]
        delay=disruption.duration_min
        out.append(RecoveryOption(f"{flight.flight_id}:delay","delay",flight.flight_id,None,delay,0,delay*self.delay_cost_per_min,(disruption.kind,)))
        for c in candidates:
            if c.flight_id==flight.flight_id:continue
            if c.origin!=flight.origin or c.destination!=flight.destination:continue
            if c.departure<disruption.at:continue
            available=max(0,c.seats)
            spill=max(0,passengers-available)
            wait=int((c.departure-flight.departure).total_seconds()//60)
            if wait<0:continue
            cost=wait*self.delay_cost_per_min+spill*self.spill_cost+1500
            notes=["reprotect"]
            if c.aircraft_family!=flight.aircraft_family:notes.append("fleet-change")
            out.append(RecoveryOption(f"{flight.flight_id}:move:{c.flight_id}","reprotect",flight.flight_id,c.flight_id,wait,spill,cost,tuple(notes)))
        out.append(RecoveryOption(f"{flight.flight_id}:cancel","cancel",flight.flight_id,None,0,passengers,self.cancel_cost+passengers*self.spill_cost,("cancelled",)))
        return tuple(sorted(out,key=lambda x:(x.cost,x.passenger_spill,x.delay_min,x.option_id)))

    def choose(self,options:Iterable[RecoveryOption],max_spill:int|None=None,max_delay_min:int|None=None)->RecoveryOption:
        filtered=[]
        for o in options:
            if max_spill is not None and o.passenger_spill>max_spill:continue
            if max_delay_min is not None and o.delay_min>max_delay_min:continue
            filtered.append(o)
        if not filtered:raise RuntimeError("no recovery option within constraints")
        return min(filtered,key=lambda o:(o.cost,o.passenger_spill,o.delay_min,o.option_id))

    def network_score(self,chosen:Iterable[RecoveryOption],connection_losses:Mapping[str,int],crew_overtime_min:Mapping[str,int])->Mapping[str,float]:
        rows=list(chosen)
        direct=sum(o.cost for o in rows)
        connection=sum(max(0,v)*220 for v in connection_losses.values())
        crew=sum(max(0,v)*1.8 for v in crew_overtime_min.values())
        cancellations=sum(1 for o in rows if o.action=="cancel")
        spill=sum(o.passenger_spill for o in rows)
        return {"direct_cost":direct,"connection_cost":connection,"crew_cost":crew,"total_cost":direct+connection+crew,"cancellations":float(cancellations),"spill":float(spill)}

    def explain(self,option:RecoveryOption)->Decision:
        if option.action=="delay":
            return Decision(True,"delay","hold original flight",{"delay_min":option.delay_min,"cost":option.cost})
        if option.action=="reprotect":
            return Decision(True,"reprotect","move customers to replacement flight",{"replacement":option.replacement_flight,"spill":option.passenger_spill,"cost":option.cost})
        return Decision(False,"cancel","cancel flight and reaccommodate later",{"spill":option.passenger_spill,"cost":option.cost})
