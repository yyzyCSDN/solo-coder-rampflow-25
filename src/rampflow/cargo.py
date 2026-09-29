from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class ULD:
    uld_id:str
    contour:str
    tare_kg:float
    gross_kg:float
    destination:str
    temperature_band:str=""
    dangerous_groups:frozenset[str]=frozenset()
    priority:int=100

@dataclass(frozen=True)
class CargoPosition:
    position_id:str
    allowed_contours:frozenset[str]
    max_gross_kg:float
    deck:str
    zone:str
    adjacent:frozenset[str]=frozenset()

@dataclass(frozen=True)
class CargoPlan:
    flight_id:str
    assignments:Mapping[str,str]
    total_gross_kg:float
    load_window:Window
    sequence:tuple[str,...]

class CargoManager:
    def __init__(self,positions:Iterable[CargoPosition]):
        self.positions={p.position_id:p for p in positions}
        self.plans:dict[str,CargoPlan]={}

    def _compatible(self,uld:ULD,pos:CargoPosition)->bool:
        return uld.contour in pos.allowed_contours and uld.gross_kg<=pos.max_gross_kg

    def _hazard_conflict(self,a:ULD,b:ULD)->bool:
        incompatible={frozenset({"explosive","flammable"}),frozenset({"oxidizer","flammable"}),frozenset({"radioactive","live-animal"})}
        return any(pair in incompatible for pair in (frozenset({x,y}) for x in a.dangerous_groups for y in b.dangerous_groups))

    def plan(self,flight_id:str,ulds:Iterable[ULD],start:datetime,loader_rate_min:int=4)->CargoPlan:
        items=sorted(ulds,key=lambda u:(u.priority,-u.gross_kg,u.uld_id))
        assignments:dict[str,str]={}
        by_position:dict[str,ULD]={}
        for uld in items:
            choices=[]
            for pos in self.positions.values():
                if pos.position_id in by_position or not self._compatible(uld,pos):continue
                bad=False
                for adj in pos.adjacent:
                    if adj in by_position and self._hazard_conflict(uld,by_position[adj]):bad=True;break
                if bad:continue
                choices.append((len(pos.adjacent),pos.deck,pos.position_id))
            if not choices:raise RuntimeError(f"no cargo position for {uld.uld_id}")
            choices.sort()
            pid=choices[0][2];assignments[uld.uld_id]=pid;by_position[pid]=uld
        sequence=tuple(u.uld_id for u in sorted(items,key=lambda u:(self.positions[assignments[u.uld_id]].deck,self.positions[assignments[u.uld_id]].zone,u.priority)))
        s=utc(start);duration=max(1,len(sequence)*loader_rate_min)
        window=Window(s,s+timedelta(minutes=duration))
        return CargoPlan(flight_id,assignments,sum(u.gross_kg for u in items),window,sequence)

    def verify(self,plan:CargoPlan,ulds:Mapping[str,ULD])->Decision:
        seen=set()
        for uid,pid in plan.assignments.items():
            if uid not in ulds or pid not in self.positions:return Decision(False,"reference","cargo plan references unknown item")
            if pid in seen:return Decision(False,"position","cargo position reused")
            seen.add(pid)
            if not self._compatible(ulds[uid],self.positions[pid]):return Decision(False,"compatibility","ULD incompatible with assigned position",{"uld":uid,"position":pid})
        for uid,pid in plan.assignments.items():
            for adj in self.positions[pid].adjacent:
                other=next((u for u,p in plan.assignments.items() if p==adj),None)
                if other and self._hazard_conflict(ulds[uid],ulds[other]):
                    return Decision(False,"hazmat-separation","adjacent dangerous cargo incompatible",{"uld":uid,"other":other})
        return Decision(True,"ok","cargo plan valid",{"gross_kg":plan.total_gross_kg})

    def commit(self,plan:CargoPlan,ulds:Mapping[str,ULD])->Decision:
        d=self.verify(plan,ulds)
        if not d.accepted:return d
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","cargo plan committed",{"sequence":plan.sequence})
