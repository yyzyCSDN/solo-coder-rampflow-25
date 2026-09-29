from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime,timedelta
from typing import Iterable,Mapping
from .timeutil import Window,utc
from .models import Decision

@dataclass(frozen=True)
class CleaningTask:
    task_id:str
    zone:str
    duration_min:int
    predecessors:frozenset[str]=frozenset()
    crew_needed:int=1
    contamination_class:str="normal"
    consumables:Mapping[str,int]=None
    def __post_init__(self):
        if self.duration_min<=0 or self.crew_needed<=0:raise ValueError("invalid cleaning task")
        if self.task_id in self.predecessors:raise ValueError("self dependency")
        if self.consumables is None:object.__setattr__(self,"consumables",{})

@dataclass(frozen=True)
class CleaningCrew:
    crew_id:str
    size:int
    capabilities:frozenset[str]
    home_zone:str

@dataclass(frozen=True)
class CleaningStep:
    task_id:str
    crew_id:str
    window:Window

@dataclass(frozen=True)
class CleaningPlan:
    flight_id:str
    steps:tuple[CleaningStep,...]
    start:datetime
    end:datetime
    consumables:Mapping[str,int]

class CleaningPlanner:
    def __init__(self,crews:Iterable[CleaningCrew],stock:Mapping[str,int]):
        self.crews={c.crew_id:c for c in crews}
        self.stock=dict(stock)
        self._crew_use:dict[str,list[Window]]={c:[] for c in self.crews}
        self.plans:dict[str,CleaningPlan]={}

    def _topological(self,tasks:Mapping[str,CleaningTask])->list[str]:
        incoming={k:set(v.predecessors) for k,v in tasks.items()}
        for tid,deps in incoming.items():
            missing=deps-set(tasks)
            if missing:raise ValueError(f"unknown predecessors for {tid}: {sorted(missing)}")
        ready=sorted(k for k,v in incoming.items() if not v)
        out=[]
        while ready:
            tid=ready.pop(0);out.append(tid)
            for other,deps in incoming.items():
                if tid in deps:
                    deps.remove(tid)
                    if not deps and other not in out and other not in ready:
                        ready.append(other);ready.sort()
        if len(out)!=len(tasks):raise ValueError("cleaning dependency cycle")
        return out

    def _crew_for(self,task:CleaningTask,earliest:datetime)->tuple[CleaningCrew,Window]:
        options=[]
        for crew in self.crews.values():
            if crew.size<task.crew_needed:continue
            if task.contamination_class!="normal" and task.contamination_class not in crew.capabilities:continue
            start=earliest
            duration=timedelta(minutes=task.duration_min)
            for w in sorted(self._crew_use[crew.crew_id],key=lambda x:x.start):
                candidate=Window(start,start+duration)
                if candidate.overlaps(w):start=w.end
            window=Window(start,start+duration)
            transfer_penalty=0 if crew.home_zone==task.zone else 4
            if transfer_penalty:
                window=Window(window.start+timedelta(minutes=transfer_penalty),window.end+timedelta(minutes=transfer_penalty))
            options.append((window.end,transfer_penalty,crew.crew_id,crew,window))
        if not options:raise RuntimeError(f"no cleaning crew for {task.task_id}")
        options.sort(key=lambda x:(x[0],x[1],x[2]))
        return options[0][3],options[0][4]

    def plan(self,flight_id:str,tasks:Iterable[CleaningTask],earliest:datetime,deadline:datetime)->CleaningPlan:
        if flight_id in self.plans:raise ValueError("cleaning already planned")
        taskmap={t.task_id:t for t in tasks}
        order=self._topological(taskmap)
        start=utc(earliest);limit=utc(deadline)
        virtual_use={k:list(v) for k,v in self._crew_use.items()}
        completion:dict[str,datetime]={}
        steps=[]
        demand:dict[str,int]={}
        for tid in order:
            task=taskmap[tid]
            dep_time=max((completion[d] for d in task.predecessors),default=start)
            options=[]
            for crew in self.crews.values():
                if crew.size<task.crew_needed:continue
                if task.contamination_class!="normal" and task.contamination_class not in crew.capabilities:continue
                cur=dep_time+(timedelta(minutes=0 if crew.home_zone==task.zone else 4))
                dur=timedelta(minutes=task.duration_min)
                while True:
                    candidate=Window(cur,cur+dur)
                    hit=[w for w in virtual_use[crew.crew_id] if w.overlaps(candidate)]
                    if not hit:break
                    cur=max(w.end for w in hit)
                options.append((candidate.end,crew.crew_id,crew,candidate))
            if not options:raise RuntimeError(f"no crew for {tid}")
            options.sort(key=lambda x:(x[0],x[1]))
            _,_,crew,window=options[0]
            if window.end>limit:raise RuntimeError("cleaning misses deadline")
            virtual_use[crew.crew_id].append(window)
            completion[tid]=window.end
            steps.append(CleaningStep(tid,crew.crew_id,window))
            for item,qty in task.consumables.items():demand[item]=demand.get(item,0)+qty
        for item,qty in demand.items():
            if self.stock.get(item,0)<qty:raise RuntimeError(f"cleaning stock shortage: {item}")
        return CleaningPlan(flight_id,tuple(steps),min(s.window.start for s in steps),max(s.window.end for s in steps),demand)

    def commit(self,plan:CleaningPlan)->Decision:
        for step in plan.steps:
            if any(w.overlaps(step.window) for w in self._crew_use[step.crew_id]):
                return Decision(False,"crew-stale","cleaning crew availability changed",{"crew":step.crew_id})
        for item,qty in plan.consumables.items():
            if self.stock.get(item,0)<qty:return Decision(False,"stock-stale","cleaning stock changed",{"item":item})
        for step in plan.steps:self._crew_use[step.crew_id].append(step.window)
        for item,qty in plan.consumables.items():self.stock[item]-=qty
        self.plans[plan.flight_id]=plan
        return Decision(True,"ok","cleaning plan committed",{"end":plan.end})

    def contamination_reclean(self,flight_id:str,zone:str,at:datetime,crew_minutes:int=20)->CleaningStep:
        plan=self.plans[flight_id]
        t=utc(at)
        options=[]
        for crew in self.crews.values():
            if "biohazard" not in crew.capabilities:continue
            cur=max(t,plan.end)
            w=Window(cur,cur+timedelta(minutes=crew_minutes))
            while any(x.overlaps(w) for x in self._crew_use[crew.crew_id]):
                cur=max(x.end for x in self._crew_use[crew.crew_id] if x.overlaps(w));w=Window(cur,cur+timedelta(minutes=crew_minutes))
            options.append((w.end,crew.crew_id,w))
        if not options:raise RuntimeError("no biohazard crew")
        options.sort()
        _,crew_id,wnd=options[0]
        self._crew_use[crew_id].append(wnd)
        return CleaningStep(f"reclean:{zone}",crew_id,wnd)
