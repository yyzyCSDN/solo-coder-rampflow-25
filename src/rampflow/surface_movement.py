from __future__ import annotations
from dataclasses import dataclass,field
from datetime import datetime,timedelta
from heapq import heappush,heappop
from typing import Iterable,Mapping
from .timeutil import Window,utc

@dataclass(frozen=True)
class TaxiEdge:
    edge_id:str
    start:str
    end:str
    travel_seconds:int
    opposite_edge:str|None=None
    max_wingspan_m:float=999.0
    crossing_group:str|None=None
    closed_windows:tuple[Window,...]=()
    def __post_init__(self):
        if self.travel_seconds<=0:raise ValueError("travel_seconds must be positive")
        if self.start==self.end:raise ValueError("self edge")

@dataclass(frozen=True)
class TaxiTraversal:
    edge_id:str
    window:Window

@dataclass(frozen=True)
class TaxiRoute:
    movement_id:str
    origin:str
    destination:str
    traversals:tuple[TaxiTraversal,...]
    planned_at:datetime
    graph_generation:int
    @property
    def start(self):return self.traversals[0].window.start if self.traversals else self.planned_at
    @property
    def end(self):return self.traversals[-1].window.end if self.traversals else self.planned_at

class TaxiConflict(RuntimeError):pass

class SurfaceRouter:
    def __init__(self,edges:Iterable[TaxiEdge],headway_seconds:int=45,crossing_headway_seconds:int=25):
        self.edges={e.edge_id:e for e in edges}
        self.headway_seconds=headway_seconds;self.crossing_headway_seconds=crossing_headway_seconds
        self._reservations:dict[str,list[tuple[str,Window]]]={k:[] for k in self.edges}
        self._routes:dict[str,TaxiRoute]={};self._generation=0
        self._adj:dict[str,list[TaxiEdge]]={}
        for edge in self.edges.values():self._adj.setdefault(edge.start,[]).append(edge)
        for values in self._adj.values():values.sort(key=lambda e:e.edge_id)

    @property
    def generation(self):return self._generation

    def close_edge(self,edge_id:str,window:Window)->None:
        edge=self.edges[edge_id]
        self.edges[edge_id]=TaxiEdge(edge.edge_id,edge.start,edge.end,edge.travel_seconds,edge.opposite_edge,edge.max_wingspan_m,edge.crossing_group,edge.closed_windows+(window,))
        self._generation+=1

    def _buffered(self,w:Window,seconds:int)->Window:
        d=timedelta(seconds=seconds);return Window(w.start-d,w.end+d)

    def _edge_free(self,edge:TaxiEdge,w:Window)->bool:
        if any(c.overlaps(w) for c in edge.closed_windows):return False
        envelope=self._buffered(w,self.headway_seconds)
        for _,used in self._reservations[edge.edge_id]:
            if envelope.overlaps(used):return False
        if edge.opposite_edge and edge.opposite_edge in self._reservations:
            for _,used in self._reservations[edge.opposite_edge]:
                if envelope.overlaps(used):return False
        if edge.crossing_group:
            cross=self._buffered(w,self.crossing_headway_seconds)
            for other in self.edges.values():
                if other.edge_id==edge.edge_id or other.crossing_group!=edge.crossing_group:continue
                for _,used in self._reservations[other.edge_id]:
                    if cross.overlaps(used):return False
        return True

    def _next_free(self,edge:TaxiEdge,start:datetime)->Window:
        start=utc(start);dur=timedelta(seconds=edge.travel_seconds)
        for _ in range(1000):
            w=Window(start,start+dur)
            blockers=[]
            blockers.extend(c for c in edge.closed_windows if c.overlaps(w))
            envelope=self._buffered(w,self.headway_seconds)
            blockers.extend(u for _,u in self._reservations[edge.edge_id] if envelope.overlaps(u))
            if edge.opposite_edge:
                blockers.extend(u for _,u in self._reservations.get(edge.opposite_edge,()) if envelope.overlaps(u))
            if edge.crossing_group:
                cross=self._buffered(w,self.crossing_headway_seconds)
                for other in self.edges.values():
                    if other.edge_id!=edge.edge_id and other.crossing_group==edge.crossing_group:
                        blockers.extend(u for _,u in self._reservations[other.edge_id] if cross.overlaps(u))
            if not blockers:return w
            start=max(b.end for b in blockers)+timedelta(seconds=max(self.headway_seconds,self.crossing_headway_seconds))
        raise TaxiConflict("unable to find free surface window")

    def plan(self,movement_id:str,origin:str,destination:str,earliest:datetime,wingspan_m:float)->TaxiRoute:
        earliest=utc(earliest)
        if movement_id in self._routes:raise ValueError("movement already committed")
        q=[];heappush(q,(earliest,origin,()))
        best:dict[str,datetime]={origin:earliest}
        while q:
            at,node,path=heappop(q)
            if node==destination:
                return TaxiRoute(movement_id,origin,destination,path,earliest,self._generation)
            if at>best.get(node,at):continue
            for edge in self._adj.get(node,()):
                if wingspan_m>edge.max_wingspan_m:continue
                w=self._next_free(edge,at)
                next_at=w.end
                if next_at<best.get(edge.end,datetime.max.replace(tzinfo=next_at.tzinfo)):
                    best[edge.end]=next_at
                    heappush(q,(next_at,edge.end,path+(TaxiTraversal(edge.edge_id,w),)))
        raise TaxiConflict(f"no route {origin}->{destination}")

    def commit(self,route:TaxiRoute)->None:
        if route.movement_id in self._routes:raise TaxiConflict("movement already committed")
        if route.graph_generation!=self._generation:
            for tr in route.traversals:
                if not self._edge_free(self.edges[tr.edge_id],tr.window):raise TaxiConflict("route became stale")
        for tr in route.traversals:
            edge=self.edges[tr.edge_id]
            if not self._edge_free(edge,tr.window):raise TaxiConflict(f"conflict on {tr.edge_id}")
        for tr in route.traversals:self._reservations[tr.edge_id].append((route.movement_id,tr.window))
        self._routes[route.movement_id]=route;self._generation+=1

    def release(self,movement_id:str,completed_edge_ids:Iterable[str]=())->None:
        completed=set(completed_edge_ids)
        route=self._routes.get(movement_id)
        if route is None:return
        if not completed:
            for edge_id in self._reservations:
                self._reservations[edge_id]=[(m,w) for m,w in self._reservations[edge_id] if m!=movement_id]
            del self._routes[movement_id]
        else:
            for edge_id in completed:
                if edge_id in self._reservations:self._reservations[edge_id]=[(m,w) for m,w in self._reservations[edge_id] if m!=movement_id]
        self._generation+=1

    def route(self,movement_id:str)->TaxiRoute:return self._routes[movement_id]
