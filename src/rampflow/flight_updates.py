from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime
from typing import Mapping
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class FlightState:
    flight_id:str
    revision:int
    scheduled_on:datetime
    scheduled_off:datetime
    estimated_on:datetime|None=None
    estimated_off:datetime|None=None
    stand_id:str|None=None
    status:str="scheduled"
    source_versions:Mapping[str,int]=None
    def __post_init__(self):
        object.__setattr__(self,"scheduled_on",utc(self.scheduled_on));object.__setattr__(self,"scheduled_off",utc(self.scheduled_off))
        if self.estimated_on is not None:object.__setattr__(self,"estimated_on",utc(self.estimated_on))
        if self.estimated_off is not None:object.__setattr__(self,"estimated_off",utc(self.estimated_off))
        if self.source_versions is None:object.__setattr__(self,"source_versions",{})

@dataclass(frozen=True)
class FlightUpdate:
    flight_id:str
    source:str
    source_version:int
    received_at:datetime
    fields:Mapping[str,object]

class FlightStateStore:
    def __init__(self):
        self.states:dict[str,FlightState]={}
        self.history:dict[str,list[FlightUpdate]]={}

    def create(self,state:FlightState)->None:
        if state.flight_id in self.states:raise ValueError("flight exists")
        self.states[state.flight_id]=state

    def apply(self,update:FlightUpdate)->Decision:
        if update.flight_id not in self.states:return Decision(False,"unknown","flight not found")
        state=self.states[update.flight_id]
        current_version=state.source_versions.get(update.source,-1)
        if update.source_version<=current_version:
            return Decision(False,"stale","source update not newer",{"current":current_version})
        allowed={"estimated_on","estimated_off","stand_id","status"}
        unknown=set(update.fields)-allowed
        if unknown:return Decision(False,"fields","update contains unsupported fields",{"fields":sorted(unknown)})
        values=dict(update.fields)
        for key in ("estimated_on","estimated_off"):
            if key in values and values[key] is not None:values[key]=utc(values[key])
        est_on=values.get("estimated_on",state.estimated_on)
        est_off=values.get("estimated_off",state.estimated_off)
        if est_on is not None and est_off is not None and est_off<=est_on:
            return Decision(False,"timeline","estimated off-block must follow on-block")
        versions=dict(state.source_versions);versions[update.source]=update.source_version
        new=replace(state,revision=state.revision+1,source_versions=versions,**values)
        self.states[update.flight_id]=new
        self.history.setdefault(update.flight_id,[]).append(update)
        return Decision(True,"applied","flight update applied",{"revision":new.revision})

    def compare_and_set(self,flight_id:str,expected_revision:int,fields:Mapping[str,object])->FlightState:
        state=self.states[flight_id]
        if state.revision!=expected_revision:raise RuntimeError("flight revision changed")
        update=FlightUpdate(flight_id,"operator",state.source_versions.get("operator",-1)+1,datetime.now(state.scheduled_on.tzinfo),fields)
        d=self.apply(update)
        if not d.accepted:raise RuntimeError(d.reason)
        return self.states[flight_id]
