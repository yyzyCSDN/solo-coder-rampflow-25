from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from heapq import heappush, heappop
from typing import Iterable, Mapping
from .timeutil import Window, utc
from .models import Decision

@dataclass(frozen=True)
class TaxiNode:
    node_id: str
    kind: str = "intersection"
    hold_allowed: bool = True

@dataclass(frozen=True)
class TaxiEdge:
    edge_id: str
    src: str
    dst: str
    length_m: float
    speed_m_s: float
    bidirectional_group: str = ""
    width_code: str = "C"
    runway_crossing: str = ""
    closed: bool = False
    def travel_seconds(self)->int:
        if self.speed_m_s <= 0 or self.length_m <= 0:
            raise ValueError("invalid taxi edge")
        return max(1,int((self.length_m/self.speed_m_s)+0.999))

@dataclass(frozen=True)
class EdgeUse:
    flight_id: str
    edge_id: str
    window: Window
    direction: tuple[str,str]
    crossing_clearance: str | None = None

@dataclass(frozen=True)
class TaxiPlan:
    flight_id: str
    nodes: tuple[str,...]
    edges: tuple[str,...]
    uses: tuple[EdgeUse,...]
    start: datetime
    end: datetime
    waiting_seconds: int

class TaxiGraph:
    def __init__(self,nodes:Iterable[TaxiNode],edges:Iterable[TaxiEdge]):
        self.nodes={n.node_id:n for n in nodes}
        self.edges={e.edge_id:e for e in edges}
        self.out:dict[str,list[TaxiEdge]]={n:[] for n in self.nodes}
        for e in self.edges.values():
            if e.src not in self.nodes or e.dst not in self.nodes:
                raise ValueError("edge endpoint missing")
            self.out[e.src].append(e)
        for values in self.out.values():
            values.sort(key=lambda e:e.edge_id)

class TaxiScheduler:
    def __init__(self,graph:TaxiGraph,separation_seconds:int=25):
        self.graph=graph
        self.separation_seconds=separation_seconds
        self._uses:dict[str,list[EdgeUse]]={}
        self._crossing_windows:dict[str,list[Window]]={}

    def set_runway_crossing_clearances(self, runway: str, windows: Iterable[Window]) -> None:
        self._crossing_windows[runway]=sorted(windows,key=lambda w:w.start)

    def _code_value(self,code:str)->int:
        order="ABCDEF"
        if code not in order:
            raise ValueError("unknown aircraft code")
        return order.index(code)

    def _edge_allowed(self,edge:TaxiEdge,aircraft_code:str)->bool:
        return not edge.closed and self._code_value(aircraft_code)<=self._code_value(edge.width_code)

    def shortest_path(self,start:str,end:str,aircraft_code:str="C",penalties:Mapping[str,float]|None=None)->tuple[str,...]:
        if start not in self.graph.nodes or end not in self.graph.nodes:
            raise KeyError("unknown taxi node")
        penalty=penalties or {}
        heap=[(0.0,start,())]
        best={start:0.0}
        while heap:
            cost,node,path=heappop(heap)
            if cost!=best.get(node):
                continue
            if node==end:
                return path
            for edge in self.graph.out[node]:
                if not self._edge_allowed(edge,aircraft_code):
                    continue
                nxt=edge.dst
                ncost=cost+edge.travel_seconds()+float(penalty.get(edge.edge_id,0))
                if ncost < best.get(nxt,float("inf")):
                    best[nxt]=ncost
                    heappush(heap,(ncost,nxt,path+(edge.edge_id,)))
        raise ValueError("no taxi path")

    def _group_conflicts(self,edge:TaxiEdge,window:Window,direction:tuple[str,str])->list[EdgeUse]:
        protected=window.expand(before=0,after=max(1,(self.separation_seconds+59)//60))
        hits=[]
        candidates=[edge.edge_id]
        if edge.bidirectional_group:
            candidates=[e.edge_id for e in self.graph.edges.values() if e.bidirectional_group==edge.bidirectional_group]
        for eid in candidates:
            for use in self._uses.get(eid,()):
                other=use.window.expand(before=0,after=max(1,(self.separation_seconds+59)//60))
                if other.overlaps(protected):
                    hits.append(use)
        return hits

    def _crossing_allowed(self,edge:TaxiEdge,window:Window)->str|None:
        if not edge.runway_crossing:
            return None
        for idx,clearance in enumerate(self._crossing_windows.get(edge.runway_crossing,())):
            if clearance.start <= window.start and window.end <= clearance.end:
                return f"{edge.runway_crossing}:{idx}"
        return None

    def plan(self,flight_id:str,start_node:str,end_node:str,start_time:datetime,aircraft_code:str="C",max_hold_seconds:int=900)->TaxiPlan:
        start=utc(start_time)
        path=self.shortest_path(start_node,end_node,aircraft_code)
        current=start
        uses=[]
        nodes=[start_node]
        waiting=0
        for eid in path:
            edge=self.graph.edges[eid]
            travel=edge.travel_seconds()
            while True:
                window=Window(current,current+timedelta(seconds=travel))
                conflicts=self._group_conflicts(edge,window,(edge.src,edge.dst))
                crossing=self._crossing_allowed(edge,window)
                if not conflicts and (not edge.runway_crossing or crossing is not None):
                    break
                if not self.graph.nodes[nodes[-1]].hold_allowed:
                    raise RuntimeError(f"cannot hold at {nodes[-1]}")
                candidates=[]
                for hit in conflicts:
                    candidates.append(hit.window.end+timedelta(seconds=self.separation_seconds))
                if edge.runway_crossing:
                    for c in self._crossing_windows.get(edge.runway_crossing,()):
                        if c.end > current and c.start >= current:
                            candidates.append(c.start)
                            break
                if not candidates:
                    raise RuntimeError("no future taxi availability")
                nxt=min(t for t in candidates if t>current)
                delta=int((nxt-current).total_seconds())
                waiting += delta
                if waiting > max_hold_seconds:
                    raise RuntimeError("taxi holding limit exceeded")
                current=nxt
            use=EdgeUse(flight_id,eid,window,(edge.src,edge.dst),crossing)
            uses.append(use)
            current=window.end
            nodes.append(edge.dst)
        return TaxiPlan(flight_id,tuple(nodes),tuple(path),tuple(uses),start,current,waiting)

    def commit(self,plan:TaxiPlan)->Decision:
        for use in plan.uses:
            edge=self.graph.edges[use.edge_id]
            conflicts=self._group_conflicts(edge,use.window,use.direction)
            if conflicts:
                return Decision(False,"taxi-conflict","taxi reservation changed",{"edge":use.edge_id,"flights":[x.flight_id for x in conflicts]})
            if edge.runway_crossing and self._crossing_allowed(edge,use.window)!=use.crossing_clearance:
                return Decision(False,"crossing-stale","runway crossing clearance no longer matches",{"edge":use.edge_id})
        for use in plan.uses:
            self._uses.setdefault(use.edge_id,[]).append(use)
            self._uses[use.edge_id].sort(key=lambda x:(x.window.start,x.window.end,x.flight_id))
        return Decision(True,"ok","taxi plan committed",{"end":plan.end})

    def release(self,flight_id:str)->int:
        removed=0
        for eid,items in list(self._uses.items()):
            keep=[x for x in items if x.flight_id!=flight_id]
            removed += len(items)-len(keep)
            if keep:self._uses[eid]=keep
            else:self._uses.pop(eid,None)
        return removed

    def occupancy(self,edge_id:str,window:Window)->tuple[EdgeUse,...]:
        return tuple(x for x in self._uses.get(edge_id,()) if x.window.overlaps(window))
