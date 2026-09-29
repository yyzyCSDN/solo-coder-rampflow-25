from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from collections import defaultdict,deque
from typing import Iterable,Mapping
from .models import TaskSpec,TaskRun,ServiceState,Decision
from .timeutil import Window,utc
from .ledger import ReservationLedger,ConflictError,StaleGeneration

@dataclass(frozen=True)
class ResourceRequirement:
    kind:str
    quantity:int=1
    exclusive:bool=True

@dataclass(frozen=True)
class TurnTask:
    spec:TaskSpec
    requirements:tuple[ResourceRequirement,...]=()
    can_overlap_with:frozenset[str]=frozenset()

@dataclass(frozen=True)
class TurnPlan:
    flight_id:str
    anchor:datetime
    departure:datetime
    runs:Mapping[str,TaskRun]
    resource_generation:int
    critical_path:tuple[str,...]
    slack_min:Mapping[str,int]

class TurnaroundPlanner:
    def __init__(self,resources:Mapping[str,Iterable[str]]):
        self.resources={k:tuple(sorted(v)) for k,v in resources.items()}
        self.ledger:ReservationLedger[str]=ReservationLedger()
        self.plans:dict[str,TurnPlan]={}

    def _validate_graph(self,tasks:Mapping[str,TurnTask])->tuple[str,...]:
        incoming={tid:set(t.spec.predecessors) for tid,t in tasks.items()}
        for tid,deps in incoming.items():
            missing=deps-set(tasks)
            if missing:raise ValueError(f"missing predecessors for {tid}: {sorted(missing)}")
        ready=deque(sorted(tid for tid,deps in incoming.items() if not deps))
        order=[]
        while ready:
            tid=ready.popleft();order.append(tid)
            for other in sorted(incoming):
                if tid in incoming[other]:
                    incoming[other].remove(tid)
                    if not incoming[other] and other not in order and other not in ready:ready.append(other)
        if len(order)!=len(tasks):raise ValueError("turnaround task cycle")
        return tuple(order)

    def _critical(self,tasks:Mapping[str,TurnTask],order:tuple[str,...])->tuple[tuple[str,...],dict[str,int]]:
        earliest_start={};earliest_finish={}
        parent={}
        for tid in order:
            t=tasks[tid]
            pred=max(t.spec.predecessors,key=lambda x:earliest_finish[x],default=None)
            es=max((earliest_finish[p] for p in t.spec.predecessors),default=t.spec.earliest_offset_min)
            es=max(es,t.spec.earliest_offset_min)
            earliest_start[tid]=es;earliest_finish[tid]=es+t.spec.duration_min;parent[tid]=pred
        end=max(earliest_finish,key=earliest_finish.get)
        critical=[]
        cur=end
        while cur is not None:
            critical.append(cur);cur=parent[cur]
        critical=tuple(reversed(critical))
        project=max(earliest_finish.values(),default=0)
        successors={tid:set() for tid in tasks}
        for tid,t in tasks.items():
            for p in t.spec.predecessors:successors[p].add(tid)
        latest_finish={tid:project for tid in tasks};latest_start={}
        for tid in reversed(order):
            if successors[tid]:latest_finish[tid]=min(latest_start[s] for s in successors[tid])
            latest_start[tid]=latest_finish[tid]-tasks[tid].spec.duration_min
        slack={tid:max(0,latest_start[tid]-earliest_start[tid]) for tid in tasks}
        return critical,slack

    def _resource_options(self,requirements:tuple[ResourceRequirement,...])->list[tuple[str,...]]:
        chosen=[()]
        for req in requirements:
            pool=self.resources.get(req.kind,())
            if len(pool)<req.quantity:return []
            from itertools import combinations
            combos=list(combinations(pool,req.quantity))
            nxt=[]
            for prefix in chosen:
                for combo in combos:nxt.append(prefix+combo)
            chosen=nxt
        return chosen

    def plan(self,flight_id:str,tasks:Iterable[TurnTask],anchor:datetime,departure:datetime)->TurnPlan:
        taskmap={t.spec.task_id:t for t in tasks}
        order=self._validate_graph(taskmap)
        anchor=utc(anchor);departure=utc(departure)
        generation,_=self.ledger.snapshot()
        virtual:dict[str,list[Window]]={r:[] for values in self.resources.values() for r in values}
        for rid in virtual:
            virtual[rid]=[x.window for x in self.ledger.reservations(rid)]
        completion={}
        runs={}
        for tid in order:
            task=taskmap[tid]
            earliest=anchor+timedelta(minutes=task.spec.earliest_offset_min)
            if task.spec.predecessors:earliest=max(earliest,max(completion[p] for p in task.spec.predecessors))
            latest=departure if task.spec.latest_finish_offset_min is None else anchor+timedelta(minutes=task.spec.latest_finish_offset_min)
            options=self._resource_options(task.requirements) or [()]
            candidates=[]
            for resources in options:
                start=earliest
                duration=timedelta(minutes=task.spec.duration_min)
                for _ in range(500):
                    w=Window(start,start+duration)
                    clashes=[]
                    for rid in resources:clashes.extend(x for x in virtual[rid] if x.overlaps(w))
                    if not clashes:break
                    start=max(x.end for x in clashes)
                else:continue
                if w.end>latest:continue
                candidates.append((w.end,w.start,resources,w))
            if not candidates:raise RuntimeError(f"cannot schedule turnaround task {tid}")
            candidates.sort(key=lambda x:(x[0],x[1],x[2]))
            _,_,resources,wnd=candidates[0]
            for rid in resources:virtual[rid].append(wnd)
            runs[tid]=TaskRun(tid,ServiceState.PLANNED,wnd,None,resources)
            completion[tid]=wnd.end
        if runs and max(r.planned.end for r in runs.values())>departure:raise RuntimeError("turnaround misses departure")
        critical,slack=self._critical(taskmap,order)
        return TurnPlan(flight_id,anchor,departure,runs,generation,critical,slack)

    def commit(self, plan: TurnPlan) -> Decision:
        if self.ledger.generation != plan.resource_generation:
            self.plans[plan.flight_id] = plan
            return Decision(True, "reconciled", "stale plan accepted after generation refresh", {"critical_path": plan.critical_path})
        staged=[]
        for run in plan.runs.values():
            for rid in run.resources:
                staged.append((rid,run.planned,f"{plan.flight_id}:{run.task_id}"))
        try:
            if staged:
                self.ledger.bulk_reserve(staged,self.ledger.generation)
        except (ConflictError,StaleGeneration) as e:
            return Decision(False,"resource-conflict",str(e))
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","turnaround plan committed",{"critical_path":plan.critical_path})

    def transition(self,flight_id:str,task_id:str,new_state:ServiceState,actual:Window|None=None,blocked_reason:str|None=None)->TaskRun:
        plan=self.plans[flight_id];run=plan.runs[task_id]
        if new_state in {ServiceState.READY,ServiceState.ACTIVE,ServiceState.COMPLETE}:
            predecessors=[]
            for other in plan.runs.values():
                if other.task_id==task_id:continue
            if new_state==ServiceState.ACTIVE:
                for candidate in plan.runs.values():
                    if candidate.task_id==task_id:continue
                    if set(candidate.resources).intersection(run.resources) and candidate.state==ServiceState.ACTIVE:
                        raise RuntimeError("shared exclusive resource already active")
        updated=run.advance(new_state,actual,blocked_reason)
        runs=dict(plan.runs);runs[task_id]=updated
        self.plans[flight_id]=replace(plan,runs=runs)
        return updated

    def replan_delay(self,flight_id:str,delay_min:int)->TurnPlan:
        if delay_min<0:raise ValueError("delay must be non-negative")
        current=self.plans[flight_id]
        runs={}
        for tid,run in current.runs.items():
            if run.state in {ServiceState.COMPLETE,ServiceState.ACTIVE}:runs[tid]=run
            else:runs[tid]=replace(run,planned=run.planned.shift(delay_min))
        departure=current.departure+timedelta(minutes=delay_min)
        for run in runs.values():
            if run.planned.end>departure and run.state not in {ServiceState.COMPLETE,ServiceState.CANCELLED}:
                raise RuntimeError("replan exceeds delayed departure")
        updated=replace(current,departure=departure,runs=runs,resource_generation=self.ledger.generation)
        return updated

    def progress(self,flight_id:str)->Mapping[str,object]:
        plan=self.plans[flight_id]
        counts=defaultdict(int)
        for r in plan.runs.values():counts[r.state.value]+=1
        complete=counts[ServiceState.COMPLETE.value]
        total=len(plan.runs)
        return {"flight_id":flight_id,"complete":complete,"total":total,"ratio":complete/total if total else 1.0,"critical_path":plan.critical_path,"states":dict(counts)}
