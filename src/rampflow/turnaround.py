from __future__ import annotations
from dataclasses import dataclass,replace
from datetime import datetime,timedelta
from collections import defaultdict,deque
from itertools import combinations
from typing import Iterable,Mapping,Callable
from .models import TaskSpec,TaskRun,ServiceState,Decision
from .timeutil import Window,utc
from .ledger import ReservationLedger,Reservation,ConflictError,StaleGeneration

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

class PlanInfeasible(RuntimeError):
    """The plan as a whole cannot be satisfied by a single coherent schedule."""

class TurnaroundPlanner:
    """Schedules one turnaround against a shared reservation ledger.

    Dependency order, task mutual-exclusion relations and resource holds are
    resolved by the same machinery used to commit a plan, so a window found in
    planning is only accepted if the whole plan can still be reserved as a
    unit.  Commit is all-or-nothing: either every resource is held or the plan
    is rejected, and a rejected plan is never recorded as feasible.
    """

    def __init__(self,resources:Mapping[str,Iterable[str]]):
        self.resources={k:tuple(sorted(v)) for k,v in resources.items()}
        self.ledger:ReservationLedger[str]=ReservationLedger()
        self.plans:dict[str,TurnPlan]={}
        self._task_defs:dict[str,dict[str,TurnTask]]={}

    # ----- graph -------------------------------------------------------
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

    # ----- one shared compatibility / scheduling scheme ----------------
    @staticmethod
    def _mutual_overlap(tasks:Mapping[str,TurnTask],a:str,b:str)->bool:
        return b in tasks[a].can_overlap_with and a in tasks[b].can_overlap_with

    def _compat(self,tasks:Mapping[str,TurnTask],flight_id:str
                )->Callable[[Reservation[str],Reservation[str]],bool]:
        """Rule used identically by the planning calendar and the ledger."""
        def compatible(incoming:Reservation[str],existing:Reservation[str])->bool:
            # A shared (non-exclusive) use never blocks another use.
            if not incoming.exclusive or not existing.exclusive:
                return True
            # Exclusive holds of the same turnaround may overlap only when both
            # tasks explicitly declare the mutual-exclusion exemption.  Check
            # the pair in both directions because the probe may point either way.
            in_owner=incoming.payload.split(":",1)
            ex_owner=existing.payload.split(":",1)
            pairs=(in_owner,ex_owner),(ex_owner,in_owner)
            for first,second in pairs:
                if len(first)==2 and len(second)==2 and first[0]==second[0]==flight_id:
                    return self._mutual_overlap(tasks,first[1],second[1])
            return False
        return compatible

    def _resource_options(self,requirements:tuple[ResourceRequirement,...]
                          )->list[tuple[tuple[tuple[str,bool],...],...]]:
        """All cross-product assignments for a task; empty list if infeasible."""
        choices:list[list[tuple[tuple[str,bool],...]]]=[]
        for req in requirements:
            pool=self.resources.get(req.kind,())
            if len(pool)<req.quantity:return []
            choices.append([tuple((rid,req.exclusive) for rid in combo)
                            for combo in combinations(pool,req.quantity)])
        assignments=[()]
        for options in choices:
            assignments=[prefix+option for prefix in assignments for option in options]
        return assignments

    def _validate_windows(self,flight_id:str,tasks:Mapping[str,TurnTask],
                          order:tuple[str,...],anchor:datetime,departure:datetime,
                          runs:Mapping[str,TaskRun],
                          holds:Mapping[str,Iterable[Reservation[str]]])->None:
        """Check a concrete set of task windows against one resource view.

        This is the single coherent feasibility test: dependency order,
        latest-finish/departure bounds, mutual-exclusion relations and every
        resource hold are evaluated together; the first violation rejects the
        whole plan.
        """
        compat=self._compat(tasks,flight_id)
        calendar:dict[str,list[Reservation[str]]]={}
        for rid in {r for values in self.resources.values() for r in values}:
            calendar[rid]=[r for r in holds.get(rid,())]
        completion:dict[str,datetime]={}
        for tid in order:
            task=tasks[tid]
            w=runs[tid].planned
            earliest=anchor+timedelta(minutes=task.spec.earliest_offset_min)
            if w.start<earliest:
                raise PlanInfeasible(f"task {tid} starts before earliest offset")
            for pred in task.spec.predecessors:
                if w.start<completion[pred]:
                    raise PlanInfeasible(f"task {tid} violates predecessor {pred}")
            latest=(departure if task.spec.latest_finish_offset_min is None
                    else anchor+timedelta(minutes=task.spec.latest_finish_offset_min))
            if w.end>min(latest,departure):
                raise PlanInfeasible(f"task {tid} exceeds latest finish / departure")
            assignment=[]
            used=set()
            for req in task.requirements:
                pool=self.resources.get(req.kind,())
                assigned=tuple(rid for rid in runs[tid].resources if rid in pool)
                if len(assigned)!=req.quantity or used.intersection(assigned):
                    raise PlanInfeasible(f"task {tid} resource assignment invalid")
                used.update(assigned)
                assignment.extend((rid,req.exclusive) for rid in assigned)
            if set(runs[tid].resources)-used:
                raise PlanInfeasible(f"task {tid} assigned unknown resource")
            for rid,req_exclusive in assignment:
                probe=Reservation(-1,rid,w,f"{flight_id}:{tid}",0,req_exclusive)
                blockers=[r for r in calendar[rid]
                          if r.window.overlaps(w) and not compat(probe,r)]
                if blockers:
                    raise PlanInfeasible(f"task {tid} blocked on {rid}")
                calendar[rid].append(probe)
            completion[tid]=w.end

    def _search(self,flight_id:str,tasks:Mapping[str,TurnTask],order:tuple[str,...],
                anchor:datetime,departure:datetime,
                holds:Mapping[str,Iterable[Reservation[str]]]
                )->dict[str,TaskRun]:
        """Find concrete windows for planning; infeasibility rejects the whole plan."""
        compat=self._compat(tasks,flight_id)
        calendar:dict[str,list[Reservation[str]]]={}
        for rid in {r for values in self.resources.values() for r in values}:
            calendar[rid]=[r for r in holds.get(rid,())]
        completion:dict[str,datetime]={}
        runs:dict[str,TaskRun]={}
        for tid in order:
            task=tasks[tid]
            earliest=anchor+timedelta(minutes=task.spec.earliest_offset_min)
            if task.spec.predecessors:
                earliest=max(earliest,max(completion[p] for p in task.spec.predecessors))
            latest=(departure if task.spec.latest_finish_offset_min is None
                    else anchor+timedelta(minutes=task.spec.latest_finish_offset_min))
            latest=min(latest,departure)
            duration=timedelta(minutes=task.spec.duration_min)
            options=self._resource_options(task.requirements) or [()]
            candidates=[]
            for assignment in options:
                start=earliest
                for _ in range(500):
                    w=Window(start,start+duration)
                    blockers=[]
                    for rid,req_exclusive in assignment:
                        probe=Reservation(-1,rid,w,f"{flight_id}:{tid}",0,req_exclusive)
                        blockers.extend(r for r in calendar[rid]
                                       if r.window.overlaps(w) and not compat(probe,r))
                    if not blockers:break
                    start=max(r.window.end for r in blockers)
                else:
                    continue
                if w.end>latest:continue
                candidates.append((w.end,w.start,assignment,w))
            if not candidates:
                raise PlanInfeasible(f"cannot schedule turnaround task {tid}")
            candidates.sort(key=lambda x:(x[0],x[1],x[2]))
            _,_,assignment,wnd=candidates[0]
            for rid,req_exclusive in assignment:
                calendar[rid].append(Reservation(-1,rid,wnd,f"{flight_id}:{tid}",0,req_exclusive))
            runs[tid]=TaskRun(tid,ServiceState.PLANNED,wnd,None,
                              tuple(rid for rid,_ in assignment))
            completion[tid]=wnd.end
        if runs and max(r.planned.end for r in runs.values())>departure:
            raise PlanInfeasible("turnaround misses departure")
        return runs

    # ----- planning / commit ------------------------------------------
    def plan(self,flight_id:str,tasks:Iterable[TurnTask],anchor:datetime,departure:datetime)->TurnPlan:
        tasklist=tuple(tasks)
        taskmap={t.spec.task_id:t for t in tasklist}
        order=self._validate_graph(taskmap)
        anchor=utc(anchor);departure=utc(departure)
        generation,_=self.ledger.snapshot()
        holds={rid:self.ledger.reservations(rid) for rid in
               {r for values in self.resources.values() for r in values}}
        runs=self._search(flight_id,taskmap,order,anchor,departure,holds)
        critical,slack=self._critical(taskmap,order)
        plan=TurnPlan(flight_id,anchor,departure,runs,generation,critical,slack)
        # definitions travel with the plan so commit re-checks the same graph
        self._task_defs[flight_id]=taskmap
        return plan

    def _build_requests(self,flight_id:str,tasks:Mapping[str,TurnTask],
                        order:tuple[str,...],runs:Mapping[str,TaskRun]
                        )->list[tuple[str,Window,str,bool]]:
        requests=[]
        for tid in order:
            run=runs[tid]
            used=set()
            for req in tasks[tid].requirements:
                pool=self.resources.get(req.kind,())
                assigned=tuple(rid for rid in run.resources if rid in pool)
                if len(assigned)!=req.quantity:
                    raise PlanInfeasible(f"task {tid} resource assignment invalid")
                if used.intersection(assigned):
                    raise PlanInfeasible(f"task {tid} resource assignment invalid")
                used.update(assigned)
                for rid in assigned:
                    requests.append((rid,run.planned,f"{flight_id}:{tid}",req.exclusive))
            extra=set(run.resources)-used
            if extra:
                raise PlanInfeasible(f"task {tid} assigned unknown resource {sorted(extra)}")
        return requests

    def commit(self, plan: TurnPlan) -> Decision:
        """Reserve the whole plan atomically or reject it as a whole.

        Dependency order, mutual-exclusion relations and resource holds are
        re-checked against the live ledger under its lock.  A stale plan is
        accepted only if exactly the same windows still constitute one
        coherent feasible schedule; otherwise it is rejected outright and no
        task, window or partial reservation is recorded.
        """
        tasks=self._task_defs.get(plan.flight_id)
        if tasks is None:
            return Decision(False,"unknown-plan","plan was not produced by this planner")
        try:
            order=self._validate_graph(tasks)
            missing=[tid for tid in order if tid not in plan.runs]
            if missing:
                raise PlanInfeasible(f"tasks missing from plan: {missing}")
            requests=self._build_requests(plan.flight_id,tasks,order,plan.runs)
        except (PlanInfeasible,ValueError) as e:
            return Decision(False,"invalid-plan",str(e))
        ledger=self.ledger
        with ledger._lock:
            if ledger.generation!=plan.resource_generation:
                return Decision(False,"stale-plan",
                                "plan resource generation is stale; replan against current resources",
                                {"plan_generation":plan.resource_generation,
                                 "current_generation":ledger.generation})
            live={rid:ledger.reservations(rid) for rid in
                  {r for values in self.resources.values() for r in values}}
            try:
                self._validate_windows(plan.flight_id,tasks,order,
                                       plan.anchor,plan.departure,plan.runs,live)
                ledger.bulk_reserve(requests,ledger.generation,
                                    compatible=self._compat(tasks,plan.flight_id))
            except PlanInfeasible as e:
                return Decision(False,"resource-conflict",str(e))
            except StaleGeneration as e:
                return Decision(False,"stale-plan",str(e))
            except ConflictError as e:
                return Decision(False,"resource-conflict",str(e))
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","turnaround plan committed",{"critical_path":plan.critical_path})

    def plan_and_commit(self,flight_id:str,tasks:Iterable[TurnTask],
                        anchor:datetime,departure:datetime)->Decision:
        """Search windows and reserve them against one resource view atomically."""
        tasklist=tuple(tasks)
        taskmap={t.spec.task_id:t for t in tasklist}
        anchor=utc(anchor);departure=utc(departure)
        ledger=self.ledger
        with ledger._lock:
            try:
                order=self._validate_graph(taskmap)
                holds={rid:ledger.reservations(rid) for rid in
                       {r for values in self.resources.values() for r in values}}
                runs=self._search(flight_id,taskmap,order,anchor,departure,holds)
                self._validate_windows(flight_id,taskmap,order,anchor,departure,runs,holds)
                requests=self._build_requests(flight_id,taskmap,order,runs)
            except (PlanInfeasible,ValueError) as e:
                return Decision(False,"infeasible",str(e))
            critical,slack=self._critical(taskmap,order)
            plan=TurnPlan(flight_id,anchor,departure,runs,ledger.generation,critical,slack)
            try:
                ledger.bulk_reserve(requests,ledger.generation,
                                    compatible=self._compat(taskmap,flight_id))
            except StaleGeneration as e:
                return Decision(False,"stale-plan",str(e))
            except ConflictError as e:
                return Decision(False,"resource-conflict",str(e))
        self.plans[flight_id]=plan
        self._task_defs[flight_id]=taskmap
        return Decision(True,"ok","turnaround plan committed",{"critical_path":plan.critical_path})

    # ----- execution ---------------------------------------------------
    def transition(self,flight_id:str,task_id:str,new_state:ServiceState,actual:Window|None=None,blocked_reason:str|None=None)->TaskRun:
        plan=self.plans[flight_id];run=plan.runs[task_id]
        tasks=self._task_defs[flight_id]
        if new_state in {ServiceState.READY,ServiceState.ACTIVE}:
            for pred in tasks[task_id].spec.predecessors:
                if plan.runs[pred].state not in {ServiceState.COMPLETE,ServiceState.CANCELLED}:
                    raise RuntimeError(f"predecessor {pred} not finished")
        if new_state==ServiceState.ACTIVE:
            for candidate in plan.runs.values():
                if candidate.task_id==task_id or candidate.state!=ServiceState.ACTIVE:continue
                shared=set(candidate.resources).intersection(run.resources)
                if shared and not self._mutual_overlap(tasks,task_id,candidate.task_id):
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
