from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from math import hypot
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class MarshalZone:
    zone_id:str
    stand_id:str
    center_x:float
    center_y:float
    safety_radius_m:float
    max_speed_kph:float

@dataclass(frozen=True)
class MarshalAgent:
    agent_id:str
    qualified_families:frozenset[str]
    zones:frozenset[str]
    shift:Window
    vision_restricted:bool=False

@dataclass(frozen=True)
class MarshalAssignment:
    flight_id:str
    agent_id:str
    zone_id:str
    window:Window
    approach_speed_kph:float

class MarshallingManager:
    def __init__(self,zones:Iterable[MarshalZone],agents:Iterable[MarshalAgent]):
        self.zones={z.zone_id:z for z in zones}
        self.agents={a.agent_id:a for a in agents}
        self._use:dict[str,list[Window]]={a:[] for a in self.agents}
        self.assignments:dict[str,MarshalAssignment]={}

    def _zone_clear(self,zone:MarshalZone,obstacles:Iterable[tuple[float,float,float]])->bool:
        for x,y,radius in obstacles:
            if hypot(x-zone.center_x,y-zone.center_y)<zone.safety_radius_m+radius:return False
        return True

    def assign(self,flight_id:str,stand_id:str,aircraft_family:str,arrival:datetime,duration_min:int,approach_speed_kph:float,obstacles:Iterable[tuple[float,float,float]]=())->MarshalAssignment:
        if flight_id in self.assignments:raise ValueError("already marshalled")
        start=utc(arrival);window=Window(start,start+timedelta(minutes=duration_min))
        zones=[z for z in self.zones.values() if z.stand_id==stand_id and self._zone_clear(z,obstacles)]
        options=[]
        for zone in zones:
            if approach_speed_kph>zone.max_speed_kph:continue
            for agent in self.agents.values():
                if aircraft_family not in agent.qualified_families and "ALL" not in agent.qualified_families:continue
                if zone.zone_id not in agent.zones:continue
                if not (agent.shift.start<=window.start and window.end<=agent.shift.end):continue
                if agent.vision_restricted and approach_speed_kph>5:continue
                if any(w.overlaps(window) for w in self._use[agent.agent_id]):continue
                options.append((agent.vision_restricted,agent.agent_id,zone.zone_id,MarshalAssignment(flight_id,agent.agent_id,zone.zone_id,window,approach_speed_kph)))
        if not options:raise RuntimeError("no marshalling assignment")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3]

    def commit(self,a:MarshalAssignment)->Decision:
        agent=self.agents[a.agent_id]
        if any(w.overlaps(a.window) for w in self._use[a.agent_id]):
            return Decision(False,"agent-stale","marshaller became occupied")
        zone=self.zones[a.zone_id]
        if a.approach_speed_kph>zone.max_speed_kph:
            return Decision(False,"speed","approach speed exceeds zone limit")
        if not (agent.shift.start<=a.window.start and a.window.end<=agent.shift.end):
            return Decision(False,"shift","marshalling outside shift")
        self._use[a.agent_id].append(a.window);self.assignments[a.flight_id]=a
        return Decision(True,"ok","marshalling committed")
