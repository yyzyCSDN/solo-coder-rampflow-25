from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class BoardingGroup:
    group_id:str
    passengers:int
    priority:int
    zone:str
    assistance:int=0
    documents_required:bool=False

@dataclass(frozen=True)
class BoardingLane:
    lane_id:str
    rate_per_min:float
    document_check_rate_per_min:float
    accessible:bool=True

@dataclass(frozen=True)
class BoardingStep:
    group_id:str
    lane_id:str
    window:Window
    passengers:int
    completed:int=0

@dataclass(frozen=True)
class BoardingPlan:
    flight_id:str
    gate_open:datetime
    door_close:datetime
    steps:tuple[BoardingStep,...]
    expected_complete:datetime

class BoardingManager:
    def __init__(self,lanes:Iterable[BoardingLane]):
        self.lanes={l.lane_id:l for l in lanes}
        self._lane_use:dict[str,list[Window]]={l:[] for l in self.lanes}
        self.plans:dict[str,BoardingPlan]={}

    def _duration(self,group:BoardingGroup,lane:BoardingLane)->int:
        if group.assistance and not lane.accessible:return 10**9
        rate=lane.document_check_rate_per_min if group.documents_required else lane.rate_per_min
        if rate<=0:return 10**9
        base=(group.passengers/rate)
        assistance=group.assistance*0.8
        return max(1,int(base+assistance+0.999))

    def plan(self,flight_id:str,groups:Iterable[BoardingGroup],gate_open:datetime,door_close:datetime)->BoardingPlan:
        start=utc(gate_open);close=utc(door_close)
        if close<=start:raise ValueError("invalid boarding window")
        virtual={k:list(v) for k,v in self._lane_use.items()}
        cursor_by_zone:dict[str,datetime]={}
        steps=[]
        ordered=sorted(groups,key=lambda g:(g.priority,g.zone,g.group_id))
        for group in ordered:
            options=[]
            earliest=max(start,cursor_by_zone.get(group.zone,start))
            for lane in self.lanes.values():
                duration=self._duration(group,lane)
                if duration>=10**9:continue
                cur=earliest
                for _ in range(200):
                    w=Window(cur,cur+timedelta(minutes=duration))
                    hit=[x for x in virtual[lane.lane_id] if x.overlaps(w)]
                    if not hit:break
                    cur=max(x.end for x in hit)
                if w.end<=close:options.append((w.end,lane.lane_id,w))
            if not options:raise RuntimeError(f"cannot board group {group.group_id}")
            options.sort(key=lambda x:(x[0],x[1]))
            _,lane_id,w=options[0]
            virtual[lane_id].append(w)
            cursor_by_zone[group.zone]=w.end
            steps.append(BoardingStep(group.group_id,lane_id,w,group.passengers))
        complete=max((s.window.end for s in steps),default=start)
        return BoardingPlan(flight_id,start,close,tuple(steps),complete)

    def commit(self,plan:BoardingPlan)->Decision:
        if plan.expected_complete>plan.door_close:
            return Decision(False,"late","boarding plan exceeds door close")
        for step in plan.steps:
            if any(w.overlaps(step.window) for w in self._lane_use[step.lane_id]):
                return Decision(False,"lane-stale","boarding lane became unavailable",{"lane":step.lane_id})
        for step in plan.steps:self._lane_use[step.lane_id].append(step.window)
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","boarding plan committed",{"complete":plan.expected_complete})

    def progress(self,flight_id:str,counts:Mapping[str,int],at:datetime)->Decision:
        plan=self.plans[flight_id];t=utc(at)
        planned={s.group_id:s.passengers for s in plan.steps}
        boarded=sum(min(max(0,counts.get(g,0)),n) for g,n in planned.items())
        total=sum(planned.values())
        if boarded>=total:
            return Decision(True,"complete","boarding complete",{"boarded":boarded,"total":total})
        remaining=total-boarded
        remaining_windows=[s.window for s in plan.steps if counts.get(s.group_id,0)<s.passengers]
        if not remaining_windows:
            return Decision(False,"count-mismatch","boarding counts inconsistent")
        latest=max(w.end for w in remaining_windows)
        status="on-track" if t<=latest else "late"
        return Decision(status=="on-track",status,"boarding progress evaluated",{"boarded":boarded,"remaining":remaining,"planned_end":latest})
