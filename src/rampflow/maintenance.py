from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta,date
from typing import Iterable,Mapping
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class Defect:
    defect_id:str
    aircraft_reg:str
    ata:str
    opened_at:datetime
    severity:str
    description:str
    repeat_key:str=""
    def __post_init__(self):
        object.__setattr__(self,"opened_at",utc(self.opened_at))

@dataclass(frozen=True)
class MELRule:
    ata:str
    category:str
    calendar_days:int|None
    flight_cycles:int|None
    required_conditions:frozenset[str]=frozenset()
    prohibited_conditions:frozenset[str]=frozenset()
    maintenance_action:str=""

@dataclass(frozen=True)
class Deferral:
    defect_id:str
    rule:MELRule
    deferred_at:datetime
    expiry_at:datetime|None
    cycles_remaining:int|None
    rectified_at:datetime|None=None

class MaintenanceControl:
    def __init__(self,rules:Iterable[MELRule],repeat_threshold:int=3,repeat_window_days:int=10):
        self.rules={r.ata:r for r in rules}
        self.repeat_threshold=repeat_threshold
        self.repeat_window_days=repeat_window_days
        self.defects:dict[str,Defect]={}
        self.deferrals:dict[str,Deferral]={}
        self.history:dict[str,list[Defect]]={}

    def report(self,defect:Defect)->Decision:
        if defect.defect_id in self.defects:return Decision(False,"duplicate","defect already exists")
        self.defects[defect.defect_id]=defect
        key=f"{defect.aircraft_reg}:{defect.repeat_key or defect.ata}"
        self.history.setdefault(key,[]).append(defect)
        cutoff=defect.opened_at-timedelta(days=self.repeat_window_days)
        recent=[d for d in self.history[key] if d.opened_at>=cutoff]
        if len(recent)>=self.repeat_threshold:
            return Decision(False,"repeat-defect","repeat threshold reached",{"count":len(recent)})
        return Decision(True,"recorded","defect recorded",{"repeat_count":len(recent)})

    def _expiry(self,opened:datetime,calendar_days:int|None)->datetime|None:
        if calendar_days is None:return None
        start_date=opened.date()
        expiry_date=start_date+timedelta(days=calendar_days)
        return datetime(expiry_date.year,expiry_date.month,expiry_date.day,23,59,59,tzinfo=opened.tzinfo)

    def defer(self,defect_id:str,at:datetime,conditions:Iterable[str],cycles_used:int=0)->Deferral:
        defect=self.defects[defect_id]
        if defect.ata not in self.rules:raise RuntimeError("no MEL relief")
        rule=self.rules[defect.ata]
        context=set(conditions)
        if not rule.required_conditions.issubset(context):raise RuntimeError("required MEL conditions missing")
        if rule.prohibited_conditions.intersection(context):raise RuntimeError("prohibited MEL condition present")
        if defect.severity=="grounding":raise RuntimeError("grounding defect cannot be deferred")
        t=utc(at)
        if t<defect.opened_at:raise ValueError("deferral before defect report")
        remaining=None if rule.flight_cycles is None else rule.flight_cycles-cycles_used
        if remaining is not None and remaining<0:raise RuntimeError("MEL cycle limit already exceeded")
        d=Deferral(defect_id,rule,t,self._expiry(t,rule.calendar_days),remaining)
        self.deferrals[defect_id]=d
        return d

    def release(self,aircraft_reg:str,departure:datetime,conditions:Iterable[str])->Decision:
        dep=utc(departure);context=set(conditions)
        active=[d for did,d in self.deferrals.items() if self.defects[did].aircraft_reg==aircraft_reg and d.rectified_at is None]
        undeferred=[d for d in self.defects.values() if d.aircraft_reg==aircraft_reg and d.defect_id not in self.deferrals]
        if undeferred:
            return Decision(False,"open-defect","aircraft has undeferred defects",{"defects":[d.defect_id for d in undeferred]})
        for d in active:
            if d.expiry_at is not None and dep>d.expiry_at:
                return Decision(False,"mel-expired","MEL calendar relief expired",{"defect":d.defect_id})
            if d.cycles_remaining is not None and d.cycles_remaining<=0:
                return Decision(False,"mel-cycles","MEL cycle relief exhausted",{"defect":d.defect_id})
            if not d.rule.required_conditions.issubset(context) or d.rule.prohibited_conditions.intersection(context):
                return Decision(False,"mel-condition","MEL operating conditions not satisfied",{"defect":d.defect_id})
        return Decision(True,"released","maintenance release conditions satisfied",{"active_deferrals":len(active)})

    def consume_cycle(self,aircraft_reg:str)->None:
        for did,d in list(self.deferrals.items()):
            if self.defects[did].aircraft_reg!=aircraft_reg or d.rectified_at is not None:continue
            if d.cycles_remaining is not None:self.deferrals[did]=replace(d,cycles_remaining=d.cycles_remaining-1)

    def rectify(self,defect_id:str,at:datetime)->Decision:
        if defect_id not in self.defects:return Decision(False,"unknown","defect not found")
        t=utc(at)
        defect=self.defects[defect_id]
        if t<defect.opened_at:return Decision(False,"time","rectification precedes defect")
        if defect_id in self.deferrals:
            self.deferrals[defect_id]=replace(self.deferrals[defect_id],rectified_at=t)
        self.defects.pop(defect_id,None)
        return Decision(True,"rectified","defect closed")
