from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class CrewMember:
    crew_id:str
    role:str
    qualifications:frozenset[str]
    base:str
    max_duty_min:int=780
    max_sectors:int=6
    min_rest_min:int=600

@dataclass(frozen=True)
class DutyLeg:
    flight_id:str
    report:datetime
    release:datetime
    origin:str
    destination:str
    aircraft_family:str
    night_minutes:int=0
    sectors:int=1
    def __post_init__(self):
        a,b=utc(self.report),utc(self.release)
        if b<=a:raise ValueError("invalid duty leg")
        object.__setattr__(self,"report",a);object.__setattr__(self,"release",b)

@dataclass(frozen=True)
class Assignment:
    crew_id:str
    leg:DutyLeg
    duty_start:datetime
    duty_end:datetime
    cumulative_duty_min:int
    sectors:int

class CrewRoster:
    def __init__(self,members:Iterable[CrewMember]):
        self.members={m.crew_id:m for m in members}
        self.assignments:dict[str,list[Assignment]]={m:[] for m in self.members}

    def _qualification(self,member:CrewMember,leg:DutyLeg)->bool:
        return leg.aircraft_family in member.qualifications or "ALL" in member.qualifications

    def _rolling_duty(self,crew_id:str,start:datetime,end:datetime,days:int)->int:
        boundary=end-timedelta(days=days)
        total=0
        for a in self.assignments[crew_id]:
            inter=a.leg
            s=max(boundary,inter.report);e=min(end,inter.release)
            if s<e:total+=int((e-s).total_seconds()//60)
        return total

    def _last_release(self,crew_id:str,before:datetime)->datetime|None:
        rows=[a.leg.release for a in self.assignments[crew_id] if a.leg.release<=before]
        return max(rows) if rows else None

    def _consecutive_sectors(self,crew_id:str,leg:DutyLeg)->int:
        rows=sorted(self.assignments[crew_id],key=lambda a:a.leg.report)
        if not rows:return leg.sectors
        last=rows[-1]
        gap=leg.report-last.leg.release
        if gap>=timedelta(hours=8):return leg.sectors
        return last.sectors+leg.sectors

    def evaluate(self,crew_id:str,leg:DutyLeg)->Decision:
        member=self.members[crew_id]
        if not self._qualification(member,leg):
            return Decision(False,"qualification","crew not qualified for aircraft")
        for a in self.assignments[crew_id]:
            if Window(a.leg.report,a.leg.release).overlaps(Window(leg.report,leg.release)):
                return Decision(False,"overlap","crew already assigned",{"flight":a.leg.flight_id})
        last=self._last_release(crew_id,leg.report)
        if last is not None:
            rest=int((leg.report-last).total_seconds()//60)
            if rest<member.min_rest_min:
                return Decision(False,"rest","minimum rest not met",{"rest_min":rest})
        duty=int((leg.release-leg.report).total_seconds()//60)
        if leg.night_minutes>=180:
            duty += 45
        if duty>member.max_duty_min:
            return Decision(False,"duty-limit","single duty exceeds limit",{"effective_min":duty})
        sectors=self._consecutive_sectors(crew_id,leg)
        if sectors>member.max_sectors:
            return Decision(False,"sector-limit","too many consecutive sectors",{"sectors":sectors})
        rolling7=self._rolling_duty(crew_id,leg.report,leg.release,7)+int((leg.release-leg.report).total_seconds()//60)
        rolling28=self._rolling_duty(crew_id,leg.report,leg.release,28)+int((leg.release-leg.report).total_seconds()//60)
        if rolling7>3600:
            return Decision(False,"rolling-7d","seven-day duty limit exceeded",{"minutes":rolling7})
        if rolling28>12000:
            return Decision(False,"rolling-28d","twenty-eight-day duty limit exceeded",{"minutes":rolling28})
        return Decision(True,"ok","crew assignment feasible",{"rolling7":rolling7,"rolling28":rolling28,"sectors":sectors})

    def assign(self,crew_id:str,leg:DutyLeg)->Assignment:
        decision=self.evaluate(crew_id,leg)
        if not decision.accepted:raise RuntimeError(decision.reason)
        rows=sorted(self.assignments[crew_id],key=lambda a:a.leg.report)
        sectors=self._consecutive_sectors(crew_id,leg)
        duty_start=leg.report
        if rows and leg.report-rows[-1].leg.release<timedelta(hours=8):
            duty_start=rows[-1].duty_start
        duty_end=leg.release
        cumulative=int((duty_end-duty_start).total_seconds()//60)
        item=Assignment(crew_id,leg,duty_start,duty_end,cumulative,sectors)
        self.assignments[crew_id].append(item)
        self.assignments[crew_id].sort(key=lambda a:a.leg.report)
        return item

    def release_flight(self,flight_id:str)->int:
        removed=0
        for crew,items in self.assignments.items():
            keep=[a for a in items if a.leg.flight_id!=flight_id]
            removed+=len(items)-len(keep)
            self.assignments[crew]=keep
        return removed
