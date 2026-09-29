from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class PushRoute:
    route_id:str
    stand_id:str
    lane:str
    release_point:str
    duration_min:int
    blocked_by:frozenset[str]=frozenset()

@dataclass(frozen=True)
class PushRequest:
    flight_id:str
    stand_id:str
    earliest:datetime
    latest:datetime
    tug_required:bool
    engine_start_min:int
    route_preferences:tuple[str,...]=()
    def __post_init__(self):
        object.__setattr__(self,"earliest",utc(self.earliest));object.__setattr__(self,"latest",utc(self.latest))

@dataclass(frozen=True)
class PushPlan:
    flight_id:str
    route_id:str
    window:Window
    engine_start:Window
    tug_id:str|None

class PushbackCoordinator:
    def __init__(self,routes:Iterable[PushRoute]):
        self.routes={r.route_id:r for r in routes}
        self._lane_use:dict[str,list[tuple[Window,str]]]={}
        self._release_use:dict[str,list[tuple[Window,str]]]={}
        self.plans:dict[str,PushPlan]={}

    def plan(self,req:PushRequest,tug_id:str|None,blocked_stands:Mapping[str,Window]|None=None)->PushPlan:
        if req.tug_required and not tug_id:raise ValueError("tug required")
        blocked=blocked_stands or {}
        routes=[self.routes[r] for r in req.route_preferences if r in self.routes] or [r for r in self.routes.values() if r.stand_id==req.stand_id]
        options=[]
        for route in routes:
            if route.stand_id!=req.stand_id:continue
            cursor=req.earliest
            for _ in range(120):
                push=Window(cursor,cursor+timedelta(minutes=route.duration_min))
                engine=Window(cursor+timedelta(minutes=1),cursor+timedelta(minutes=max(2,req.engine_start_min+1)))
                conflict=[]
                conflict.extend(w for w,_ in self._lane_use.get(route.lane,()) if w.overlaps(push))
                conflict.extend(w for w,_ in self._release_use.get(route.release_point,()) if w.overlaps(push.expand(after=2)))
                for stand in route.blocked_by:
                    if stand in blocked and blocked[stand].overlaps(push):conflict.append(blocked[stand])
                if not conflict:break
                cursor=max(w.end for w in conflict)
            if push.end<=req.latest:options.append((push.end,route.route_id,PushPlan(req.flight_id,route.route_id,push,engine,tug_id)))
        if not options:raise RuntimeError("no pushback slot")
        options.sort(key=lambda x:(x[0],x[1]))
        return options[0][2]

    def commit(self,plan:PushPlan)->Decision:
        route=self.routes[plan.route_id]
        if any(w.overlaps(plan.window) for w,_ in self._lane_use.get(route.lane,())):
            return Decision(False,"lane-stale","pushback lane changed")
        protected=plan.window.expand(after=2)
        if any(w.overlaps(protected) for w,_ in self._release_use.get(route.release_point,())):
            return Decision(False,"release-stale","release point changed")
        self._lane_use.setdefault(route.lane,[]).append((plan.window,plan.flight_id))
        self._release_use.setdefault(route.release_point,[]).append((protected,plan.flight_id))
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","pushback committed",{"route":plan.route_id})

    def cancel(self,flight_id:str)->bool:
        p=self.plans.pop(flight_id,None)
        if p is None:return False
        r=self.routes[p.route_id]
        self._lane_use[r.lane]=[(w,f) for w,f in self._lane_use.get(r.lane,()) if f!=flight_id]
        self._release_use[r.release_point]=[(w,f) for w,f in self._release_use.get(r.release_point,()) if f!=flight_id]
        return True
