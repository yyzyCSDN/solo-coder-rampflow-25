from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Mapping
from .models import Decision

@dataclass(frozen=True)
class Compartment:
    compartment_id:str
    arm_m:float
    max_kg:float
    floor_limit_kg_m2:float
    area_m2:float
    allowed_kinds:frozenset[str]=frozenset({"bags","cargo","mail"})
    def capacity(self)->float:
        return min(self.max_kg,self.floor_limit_kg_m2*self.area_m2)

@dataclass(frozen=True)
class LoadItem:
    item_id:str
    kind:str
    weight_kg:float
    priority:int=100
    compartment_hint:str|None=None
    dangerous_group:str=""
    connection_group:str=""

@dataclass(frozen=True)
class EnvelopePoint:
    weight_kg:float
    min_index:float
    max_index:float

@dataclass(frozen=True)
class LoadSheet:
    aircraft_reg:str
    empty_weight_kg:float
    empty_index:float
    fuel_kg:float
    fuel_arm_m:float
    allocations:Mapping[str,tuple[LoadItem,...]]
    takeoff_weight_kg:float
    takeoff_index:float
    landing_weight_kg:float
    landing_index:float
    trim_units:float

class LoadControl:
    def __init__(self,compartments:Iterable[Compartment],envelope:Iterable[EnvelopePoint],datum_m:float=0.0):
        self.compartments={c.compartment_id:c for c in compartments}
        self.envelope=sorted(envelope,key=lambda p:p.weight_kg)
        self.datum_m=datum_m
        if len(self.envelope)<2:raise ValueError("envelope requires points")

    def _index_from_moment(self,moment_kg_m:float)->float:
        return moment_kg_m/1000.0

    def _bounds(self,weight:float)->tuple[float,float]:
        pts=self.envelope
        if weight<pts[0].weight_kg or weight>pts[-1].weight_kg:
            raise ValueError("weight outside CG envelope")
        for a,b in zip(pts,pts[1:]):
            if a.weight_kg<=weight<=b.weight_kg:
                ratio=(weight-a.weight_kg)/(b.weight_kg-a.weight_kg)
                return (a.min_index+(b.min_index-a.min_index)*ratio,a.max_index+(b.max_index-a.max_index)*ratio)
        return pts[-1].min_index,pts[-1].max_index

    def _dangerous_compatible(self,items:tuple[LoadItem,...],candidate:LoadItem)->bool:
        groups={x.dangerous_group for x in items if x.dangerous_group}
        if not candidate.dangerous_group:return True
        incompatible={frozenset({"oxidizer","flammable"}),frozenset({"acid","cyanide"})}
        return all(frozenset({candidate.dangerous_group,g}) not in incompatible for g in groups)

    def allocate(self,items:Iterable[LoadItem])->dict[str,tuple[LoadItem,...]]:
        alloc={cid:[] for cid in self.compartments}
        ordered=sorted(items,key=lambda x:(x.priority,-x.weight_kg,x.item_id))
        for item in ordered:
            choices=[]
            for cid,c in self.compartments.items():
                if item.kind not in c.allowed_kinds:continue
                if item.compartment_hint and cid!=item.compartment_hint:continue
                current=sum(x.weight_kg for x in alloc[cid])
                if current+item.weight_kg>c.capacity()+1e-9:continue
                if not self._dangerous_compatible(tuple(alloc[cid]),item):continue
                choices.append((c.capacity()-current-item.weight_kg,abs(c.arm_m-self.datum_m),cid))
            if not choices:
                raise RuntimeError(f"cannot allocate {item.item_id}")
            choices.sort()
            alloc[choices[0][2]].append(item)
        return {k:tuple(v) for k,v in alloc.items()}

    def build(self,aircraft_reg:str,empty_weight_kg:float,empty_index:float,fuel_kg:float,fuel_arm_m:float,burn_kg:float,items:Iterable[LoadItem])->LoadSheet:
        if burn_kg<0 or burn_kg>fuel_kg:raise ValueError("invalid planned burn")
        allocations=self.allocate(items)
        payload=sum(i.weight_kg for rows in allocations.values() for i in rows)
        payload_moment=sum(i.weight_kg*self.compartments[cid].arm_m for cid,rows in allocations.items() for i in rows)
        empty_moment=empty_index*1000.0
        takeoff_weight=empty_weight_kg+payload+fuel_kg
        takeoff_moment=empty_moment+payload_moment+fuel_kg*fuel_arm_m
        takeoff_index=self._index_from_moment(takeoff_moment)
        landing_fuel=fuel_kg-burn_kg
        landing_weight=empty_weight_kg+payload+landing_fuel
        landing_moment=empty_moment+payload_moment+landing_fuel*fuel_arm_m
        landing_index=self._index_from_moment(landing_moment)
        for weight,index,label in ((takeoff_weight,takeoff_index,"takeoff"),(landing_weight,landing_index,"landing")):
            lo,hi=self._bounds(weight)
            if not lo<=index<=hi:
                raise RuntimeError(f"{label} CG outside envelope")
        lo,hi=self._bounds(takeoff_weight)
        center=(lo+hi)/2
        span=max(1e-6,hi-lo)
        trim=(takeoff_index-center)/span*10
        return LoadSheet(aircraft_reg,empty_weight_kg,empty_index,fuel_kg,fuel_arm_m,allocations,takeoff_weight,takeoff_index,landing_weight,landing_index,trim)

    def verify(self,sheet:LoadSheet)->Decision:
        for cid,items in sheet.allocations.items():
            c=self.compartments[cid]
            total=sum(i.weight_kg for i in items)
            if total>c.capacity()+1e-6:
                return Decision(False,"compartment-overweight","compartment capacity exceeded",{"compartment":cid})
            for i in items:
                if i.kind not in c.allowed_kinds:
                    return Decision(False,"kind","item in incompatible compartment",{"item":i.item_id})
        for weight,index,label in ((sheet.takeoff_weight_kg,sheet.takeoff_index,"takeoff"),(sheet.landing_weight_kg,sheet.landing_index,"landing")):
            try:lo,hi=self._bounds(weight)
            except ValueError:return Decision(False,"weight-envelope",f"{label} weight outside envelope")
            if not lo<=index<=hi:return Decision(False,"cg-envelope",f"{label} CG outside envelope")
        return Decision(True,"ok","load sheet valid",{"trim":sheet.trim_units})
