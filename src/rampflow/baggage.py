from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime,timedelta
from heapq import heappush,heappop
from typing import Iterable, Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class Belt:
    belt_id:str
    area:str
    capacity_bags_per_min:int
    destinations:frozenset[str]=frozenset()
    international_screening:bool=False
    oversized:bool=False

@dataclass(frozen=True)
class TransferLink:
    src:str
    dst:str
    travel_min:int
    capacity_bags:int
    security_transition:str=""
    active:bool=True

@dataclass(frozen=True)
class BagBatch:
    batch_id:str
    flight_id:str
    count:int
    ready_at:datetime
    destination:str
    international:bool=False
    oversized:bool=False
    connection_departure:datetime|None=None
    min_connection_min:int=0
    def __post_init__(self):
        object.__setattr__(self,"ready_at",utc(self.ready_at))
        if self.connection_departure is not None:
            object.__setattr__(self,"connection_departure",utc(self.connection_departure))
        if self.count<=0:raise ValueError("bag count must be positive")

@dataclass(frozen=True)
class BagPlan:
    batch_id:str
    belt_id:str
    route:tuple[str,...]
    induction:Window
    arrival_at_makeup:datetime
    cutoff:datetime
    screening_required:bool
    feasible:bool
    reasons:tuple[str,...]=()

class BaggageSystem:
    def __init__(self,belts:Iterable[Belt],links:Iterable[TransferLink]):
        self.belts={b.belt_id:b for b in belts}
        self.links=tuple(links)
        self.graph:dict[str,list[TransferLink]]={}
        for link in self.links:
            self.graph.setdefault(link.src,[]).append(link)
        self._belt_use:dict[str,list[tuple[Window,int]]]={b:[] for b in self.belts}
        self._link_use:dict[tuple[str,str],list[tuple[Window,int]]]={}
        self.plans:dict[str,BagPlan]={}

    def _belt_candidates(self,batch:BagBatch)->list[Belt]:
        out=[]
        for b in self.belts.values():
            if b.destinations and batch.destination not in b.destinations:continue
            if batch.oversized and not b.oversized:continue
            if batch.international and not b.international_screening:continue
            out.append(b)
        return sorted(out,key=lambda b:(b.area,b.belt_id))

    def _route(self,start_area:str,destination:str,batch:BagBatch)->tuple[str,...]:
        heap=[(0,start_area,())]
        best={start_area:0}
        while heap:
            cost,node,path=heappop(heap)
            if cost!=best[node]:continue
            if node==destination:return path
            for link in self.graph.get(node,()):
                if not link.active:continue
                extra=0
                if batch.international and link.security_transition=="unscreened-to-secure":
                    extra=4
                ncost=cost+link.travel_min+extra
                if ncost<best.get(link.dst,10**9):
                    best[link.dst]=ncost
                    heappush(heap,(ncost,link.dst,path+(f"{link.src}>{link.dst}",)))
        raise RuntimeError("no baggage route")

    def _link_for(self,key:str)->TransferLink:
        src,dst=key.split(">",1)
        for link in self.links:
            if link.src==src and link.dst==dst:
                return link
        raise KeyError(key)

    def _induction_slot(self,belt:Belt,batch:BagBatch)->Window:
        duration=max(1,(batch.count+belt.capacity_bags_per_min-1)//belt.capacity_bags_per_min)
        cursor=batch.ready_at
        for _ in range(240):
            candidate=Window(cursor,cursor+timedelta(minutes=duration))
            overlaps=[x for x in self._belt_use[belt.belt_id] if x[0].overlaps(candidate)]
            if not overlaps:
                return candidate
            cursor=max(w.end for w,_ in overlaps)
        raise RuntimeError("belt capacity horizon exhausted")

    def plan(self,batch:BagBatch,start_area:str,makeup_area:str,cutoff_before_departure_min:int=20)->BagPlan:
        if batch.batch_id in self.plans:
            raise ValueError("batch already planned")
        belts=self._belt_candidates(batch)
        if not belts:
            return BagPlan(batch.batch_id,"",(),Window(batch.ready_at,batch.ready_at+timedelta(minutes=1)),batch.ready_at,batch.ready_at,False,False,("no-compatible-belt",))
        route=self._route(start_area,makeup_area,batch)
        best=None
        for belt in belts:
            induction=self._induction_slot(belt,batch)
            cur=induction.end
            screening=False
            feasible=True
            reasons=[]
            for key in route:
                link=self._link_for(key)
                duration=timedelta(minutes=link.travel_min)
                w=Window(cur,cur+duration)
                used=sum(count for win,count in self._link_use.get((link.src,link.dst),()) if win.overlaps(w))
                if used+batch.count>link.capacity_bags:
                    feasible=False;reasons.append(f"link-capacity:{key}")
                if batch.international and link.security_transition=="unscreened-to-secure":
                    screening=True
                    cur += timedelta(minutes=4)
                cur += duration
            if batch.connection_departure is not None:
                cutoff=batch.connection_departure-timedelta(minutes=max(cutoff_before_departure_min,batch.min_connection_min))
            else:
                cutoff=cur+timedelta(hours=4)
            if cur>cutoff:
                feasible=False;reasons.append("missed-cutoff")
            score=(not feasible,cur,induction.start,belt.belt_id)
            candidate=(score,BagPlan(batch.batch_id,belt.belt_id,route,induction,cur,cutoff,screening,feasible,tuple(reasons)))
            if best is None or candidate[0]<best[0]:best=candidate
        return best[1]

    def commit(self,batch:BagBatch,plan:BagPlan)->Decision:
        if plan.batch_id!=batch.batch_id:raise ValueError("plan mismatch")
        if not plan.feasible:
            return Decision(False,"infeasible","baggage plan cannot be committed",{"reasons":plan.reasons})
        belt=self.belts[plan.belt_id]
        overlaps=[x for x in self._belt_use[plan.belt_id] if x[0].overlaps(plan.induction)]
        if overlaps:
            return Decision(False,"belt-stale","belt slot no longer available")
        cur=plan.induction.end
        staged=[]
        for key in plan.route:
            link=self._link_for(key)
            w=Window(cur,cur+timedelta(minutes=link.travel_min))
            used=sum(count for win,count in self._link_use.get((link.src,link.dst),()) if win.overlaps(w))
            if used+batch.count>link.capacity_bags:
                return Decision(False,"link-stale","transfer link capacity changed",{"link":key})
            staged.append((link,w))
            if batch.international and link.security_transition=="unscreened-to-secure":
                cur += timedelta(minutes=4)
            cur=w.end if cur<=w.end else cur
        self._belt_use[plan.belt_id].append((plan.induction,batch.count))
        for link,w in staged:
            self._link_use.setdefault((link.src,link.dst),[]).append((w,batch.count))
        self.plans[batch.batch_id]=plan
        return Decision(True,"ok","baggage plan committed",{"arrival":plan.arrival_at_makeup})

    def connection_status(self,batch_id:str,actual_departure:datetime)->Decision:
        plan=self.plans[batch_id]
        dep=utc(actual_departure)
        margin=int((dep-plan.arrival_at_makeup).total_seconds()//60)
        if margin<0:return Decision(False,"bags-late","bags arrive after departure",{"margin_min":margin})
        if plan.arrival_at_makeup>plan.cutoff:return Decision(False,"cutoff-missed","bags reached makeup after contractual cutoff",{"margin_min":margin})
        return Decision(True,"connected","bag transfer meets cutoff",{"margin_min":margin})
