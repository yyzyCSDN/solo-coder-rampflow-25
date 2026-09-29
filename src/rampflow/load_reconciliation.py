from __future__ import annotations
from dataclasses import dataclass,replace
from typing import Iterable,Mapping

@dataclass(frozen=True)
class Compartment:
    name:str;max_weight_kg:float;arm_m:float;allowed_categories:frozenset[str]=frozenset({'bag','cargo','mail'})

@dataclass(frozen=True)
class LoadItem:
    item_id:str;weight_kg:float;category:str;destination:str;priority:int=0;hazmat_code:str|None=None

@dataclass(frozen=True)
class LoadPlan:
    assignments:Mapping[str,tuple[LoadItem,...]]
    total_weight_kg:float
    moment_kg_m:float
    trim_index:float
    version:int=1

class LoadPlanner:
    def __init__(self,compartments:Iterable[Compartment],target_trim:float,trim_min:float,trim_max:float):
        self.compartments={c.name:c for c in compartments};self.target_trim=target_trim;self.trim_min=trim_min;self.trim_max=trim_max
        if not self.compartments:raise ValueError('compartments required')

    def _compatible(self,item:LoadItem,compartment:Compartment,assigned:Iterable[LoadItem])->bool:
        if item.category not in compartment.allowed_categories:return False
        existing=list(assigned)
        if item.hazmat_code:
            forbidden={'OX':{'FL','GA'},'FL':{'OX'},'GA':{'OX'}}
            if any(x.hazmat_code in forbidden.get(item.hazmat_code,set()) for x in existing):return False
        return True

    def _score(self,assignments:Mapping[str,list[LoadItem]])->tuple[float,float,float]:
        total=sum(i.weight_kg for xs in assignments.values() for i in xs)
        moment=sum(i.weight_kg*self.compartments[name].arm_m for name,xs in assignments.items() for i in xs)
        trim=moment/total if total else self.target_trim
        return total,moment,trim

    def plan(self,items:Iterable[LoadItem])->LoadPlan:
        assignments={name:[] for name in self.compartments}
        used={name:0.0 for name in self.compartments}
        ordered=sorted(items,key=lambda i:(i.hazmat_code is None,-i.priority,-i.weight_kg,i.item_id))
        for item in ordered:
            candidates=[]
            for name,c in self.compartments.items():
                if used[name]+item.weight_kg>c.max_weight_kg:continue
                if not self._compatible(item,c,assignments[name]):continue
                assignments[name].append(item);_,_,trim=self._score(assignments);assignments[name].pop()
                candidates.append((abs(trim-self.target_trim),used[name]/c.max_weight_kg,name))
            if not candidates:raise RuntimeError(f'cannot place load item {item.item_id}')
            name=min(candidates)[2];assignments[name].append(item);used[name]+=item.weight_kg
        total,moment,trim=self._score(assignments)
        if not self.trim_min<=trim<=self.trim_max:raise RuntimeError('load plan outside trim envelope')
        return LoadPlan({k:tuple(v) for k,v in assignments.items()},total,moment,trim)

    def reconcile(self,plan:LoadPlan,removed_ids:Iterable[str],added:Iterable[LoadItem])->LoadPlan:
        removed=set(removed_ids);remaining=[i for xs in plan.assignments.values() for i in xs if i.item_id not in removed]
        remaining.extend(added)
        updated=self.plan(remaining)
        return replace(updated,version=plan.version+1)

    def manifest(self,plan:LoadPlan)->tuple[tuple[str,str,float],...]:
        return tuple(sorted((i.item_id,name,i.weight_kg) for name,xs in plan.assignments.items() for i in xs))
