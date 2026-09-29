from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from math import hypot
from typing import Iterable
from .timeutil import Window
from .models import Decision

@dataclass(frozen=True)
class Bridge:
    bridge_id:str
    stand_id:str
    min_height_m:float
    max_height_m:float
    min_reach_m:float
    max_reach_m:float
    swing_radius_m:float
    dual_capable:bool=False

@dataclass(frozen=True)
class DoorGeometry:
    door_id:str
    height_m:float
    offset_x_m:float
    offset_y_m:float
    preferred:bool=False

@dataclass(frozen=True)
class BridgeMove:
    bridge_id:str
    door_id:str
    window:Window
    target_height_m:float
    reach_m:float

class JetBridgeManager:
    def __init__(self,bridges:Iterable[Bridge]):
        self.bridges={b.bridge_id:b for b in bridges}
        self._use:dict[str,list[BridgeMove]]={b:[] for b in self.bridges}
        self.assignments:dict[str,tuple[BridgeMove,...]]={}

    def _compatible(self,b:Bridge,d:DoorGeometry)->bool:
        reach=hypot(d.offset_x_m,d.offset_y_m)
        return b.min_height_m<=d.height_m<=b.max_height_m and b.min_reach_m<=reach<=b.max_reach_m

    def plan(self,flight_id:str,stand_id:str,doors:Iterable[DoorGeometry],arrival:datetime,service_min:int,dual:bool=False)->tuple[BridgeMove,...]:
        door_list=sorted(doors,key=lambda d:(not d.preferred,d.door_id))
        candidates=[b for b in self.bridges.values() if b.stand_id==stand_id and (not dual or b.dual_capable)]
        if not candidates:raise RuntimeError("no bridge at stand")
        start=arrival;window=Window(start,start+timedelta(minutes=service_min))
        options=[]
        for b in candidates:
            if any(m.window.overlaps(window) for m in self._use[b.bridge_id]):continue
            for d in door_list:
                if not self._compatible(b,d):continue
                reach=hypot(d.offset_x_m,d.offset_y_m)
                options.append((not d.preferred,reach,b.bridge_id,d.door_id,BridgeMove(b.bridge_id,d.door_id,window,d.height_m,reach)))
        if not options:raise RuntimeError("no bridge-door geometry")
        options.sort(key=lambda x:(x[0],x[1],x[2],x[3]))
        first=options[0][4]
        moves=[first]
        if dual:
            for opt in options[1:]:
                m=opt[4]
                if m.bridge_id==first.bridge_id or m.door_id==first.door_id:continue
                b1=self.bridges[first.bridge_id];b2=self.bridges[m.bridge_id]
                if abs(first.reach_m-m.reach_m)<min(b1.swing_radius_m,b2.swing_radius_m)/4:continue
                moves.append(m);break
            if len(moves)<2:raise RuntimeError("dual bridge requested but second bridge unsafe")
        return tuple(moves)

    def commit(self,flight_id:str,moves:tuple[BridgeMove,...])->Decision:
        for move in moves:
            if any(x.window.overlaps(move.window) for x in self._use[move.bridge_id]):
                return Decision(False,"bridge-stale","jet bridge became occupied",{"bridge":move.bridge_id})
        if len({m.door_id for m in moves})!=len(moves):
            return Decision(False,"door","multiple bridges target same door")
        for move in moves:self._use[move.bridge_id].append(move)
        self.assignments[flight_id]=moves
        return Decision(True,"ok","jet bridge assignment committed",{"bridges":[m.bridge_id for m in moves]})

    def disconnect(self,flight_id:str,at:datetime)->int:
        moves=self.assignments.pop(flight_id,())
        changed=0
        for m in moves:
            keep=[]
            for old in self._use[m.bridge_id]:
                if old==m:
                    if old.window.start<at:
                        keep.append(BridgeMove(old.bridge_id,old.door_id,Window(old.window.start,at),old.target_height_m,old.reach_m))
                    changed+=1
                else:keep.append(old)
            self._use[m.bridge_id]=keep
        return changed
