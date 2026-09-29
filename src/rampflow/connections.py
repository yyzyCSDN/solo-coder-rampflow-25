from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Mapping,Iterable
from .timeutil import utc
from .models import Decision

@dataclass(frozen=True)
class ConnectionRule:
    inbound_type:str
    outbound_type:str
    terminal_pair:tuple[str,str]
    minimum_min:int
    security_min:int=0
    border_min:int=0
    baggage_extra_min:int=0
    mobility_extra_min:int=0

@dataclass(frozen=True)
class Connection:
    connection_id:str
    inbound_flight:str
    outbound_flight:str
    arrival:datetime
    departure:datetime
    inbound_terminal:str
    outbound_terminal:str
    inbound_type:str
    outbound_type:str
    passengers:int
    checked_bags:int
    mobility_passengers:int=0
    protected:bool=True
    def __post_init__(self):
        object.__setattr__(self,"arrival",utc(self.arrival));object.__setattr__(self,"departure",utc(self.departure))
        if self.departure<=self.arrival:raise ValueError("connection departure must follow arrival")

@dataclass(frozen=True)
class ReaccommodationOption:
    flight_id:str
    departure:datetime
    seats:int
    terminal:str
    same_day:bool=True
    baggage_retag_min:int=8

class ConnectionManager:
    def __init__(self,rules:Iterable[ConnectionRule],terminal_transfer_min:Mapping[tuple[str,str],int]):
        self.rules=tuple(rules)
        self.transfer=dict(terminal_transfer_min)
        self.connections:dict[str,Connection]={}

    def _rule(self,c:Connection)->ConnectionRule:
        exact=[]
        for r in self.rules:
            if r.inbound_type not in {c.inbound_type,"*"}:continue
            if r.outbound_type not in {c.outbound_type,"*"}:continue
            if r.terminal_pair not in {(c.inbound_terminal,c.outbound_terminal),("*","*")}:continue
            specificity=(r.inbound_type!="*")+(r.outbound_type!="*")+(r.terminal_pair!=("*","*"))
            exact.append((specificity,r))
        if not exact:raise RuntimeError("no connection rule")
        exact.sort(key=lambda x:-x[0])
        return exact[0][1]

    def required_minutes(self,c:Connection)->int:
        r=self._rule(c)
        walking=self.transfer.get((c.inbound_terminal,c.outbound_terminal),0 if c.inbound_terminal==c.outbound_terminal else 15)
        total=r.minimum_min+walking+r.security_min+r.border_min
        if c.checked_bags:total+=r.baggage_extra_min
        if c.mobility_passengers:total+=r.mobility_extra_min
        return total

    def status(self,c:Connection,gate_in_delay_min:int=0,gate_out_delay_min:int=0)->Decision:
        arrival=c.arrival+timedelta(minutes=gate_in_delay_min)
        departure=c.departure+timedelta(minutes=gate_out_delay_min)
        available=int((departure-arrival).total_seconds()//60)
        required=self.required_minutes(c)
        margin=available-required
        if margin<0:
            return Decision(False,"misconnect","connection below minimum",{"available_min":available,"required_min":required,"margin_min":margin})
        risk="tight" if margin<10 else "comfortable"
        return Decision(True,risk,"connection feasible",{"available_min":available,"required_min":required,"margin_min":margin})

    def protect(self,c:Connection)->None:
        if c.connection_id in self.connections:raise ValueError("duplicate connection")
        self.connections[c.connection_id]=c

    def reaccommodate(self,connection_id:str,options:Iterable[ReaccommodationOption],current_time:datetime)->ReaccommodationOption:
        c=self.connections[connection_id];now=utc(current_time)
        candidates=[]
        for opt in options:
            dep=utc(opt.departure)
            if dep<=now:continue
            if opt.seats<c.passengers:continue
            transfer=self.transfer.get((c.inbound_terminal,opt.terminal),0 if c.inbound_terminal==opt.terminal else 15)
            ready=max(now,c.arrival)+timedelta(minutes=transfer+(opt.baggage_retag_min if c.checked_bags else 0))
            if ready>dep:continue
            overnight=dep.date()!=c.departure.date()
            candidates.append((overnight,dep,opt.flight_id,opt))
        if not candidates:raise RuntimeError("no reaccommodation")
        candidates.sort(key=lambda x:(x[0],x[1],x[2]))
        return candidates[0][3]

    def bank_impact(self,inbound_delay:Mapping[str,int],outbound_delay:Mapping[str,int])->Mapping[str,Decision]:
        out={}
        for cid,c in self.connections.items():
            out[cid]=self.status(c,inbound_delay.get(c.inbound_flight,0),outbound_delay.get(c.outbound_flight,0))
        return out
