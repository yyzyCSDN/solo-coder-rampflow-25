from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Generic, TypeVar, Iterable
from .timeutil import Window, utc

T=TypeVar("T")

@dataclass(frozen=True)
class Reservation(Generic[T]):
    token: int
    resource_id: str
    window: Window
    payload: T
    generation: int

class ConflictError(RuntimeError):
    pass

class StaleGeneration(RuntimeError):
    pass

class ReservationLedger(Generic[T]):
    def __init__(self):
        self._lock=RLock()
        self._by_resource: dict[str,list[Reservation[T]]]={}
        self._generation=0
        self._next_token=1
    @property
    def generation(self)->int:
        with self._lock:
            return self._generation
    def snapshot(self)->tuple[int,tuple[Reservation[T],...]]:
        with self._lock:
            rows=[x for items in self._by_resource.values() for x in items]
            rows.sort(key=lambda r:(r.resource_id,r.window.start,r.window.end,r.token))
            return self._generation,tuple(rows)
    def reservations(self,resource_id:str)->tuple[Reservation[T],...]:
        with self._lock:
            return tuple(self._by_resource.get(resource_id,()))
    def conflicts(self,resource_id:str,window:Window)->tuple[Reservation[T],...]:
        with self._lock:
            return tuple(r for r in self._by_resource.get(resource_id,()) if r.window.overlaps(window))
    def reserve(self,resource_id:str,window:Window,payload:T,expected_generation:int|None=None)->Reservation[T]:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise StaleGeneration(f"expected {expected_generation}, actual {self._generation}")
            conflicts=[r for r in self._by_resource.get(resource_id,()) if r.window.overlaps(window)]
            if conflicts:
                raise ConflictError(f"{resource_id} unavailable")
            item=Reservation(self._next_token,resource_id,window,payload,self._generation+1)
            self._next_token += 1
            self._generation += 1
            self._by_resource.setdefault(resource_id,[]).append(item)
            self._by_resource[resource_id].sort(key=lambda r:(r.window.start,r.window.end,r.token))
            return item
    def replace(self,token:int,new_window:Window,expected_generation:int)->Reservation[T]:
        with self._lock:
            if expected_generation != self._generation:
                raise StaleGeneration("ledger changed")
            found=None
            for rid,items in self._by_resource.items():
                for item in items:
                    if item.token==token:
                        found=(rid,item)
                        break
                if found:
                    break
            if found is None:
                raise KeyError(token)
            rid,item=found
            conflicts=[r for r in self._by_resource[rid] if r.token != token and r.window.overlaps(new_window)]
            if conflicts:
                raise ConflictError(f"{rid} unavailable")
            updated=Reservation(item.token,rid,new_window,item.payload,self._generation+1)
            self._by_resource[rid]=[updated if r.token==token else r for r in self._by_resource[rid]]
            self._by_resource[rid].sort(key=lambda r:(r.window.start,r.window.end,r.token))
            self._generation += 1
            return updated
    def release(self,token:int,expected_generation:int|None=None)->bool:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise StaleGeneration("ledger changed")
            for rid,items in list(self._by_resource.items()):
                kept=[r for r in items if r.token != token]
                if len(kept)!=len(items):
                    if kept:
                        self._by_resource[rid]=kept
                    else:
                        del self._by_resource[rid]
                    self._generation += 1
                    return True
            return False
    def bulk_reserve(self,requests:Iterable[tuple[str,Window,T]],expected_generation:int|None=None)->tuple[Reservation[T],...]:
        with self._lock:
            if expected_generation is not None and expected_generation != self._generation:
                raise StaleGeneration("ledger changed")
            req=list(requests)
            shadow={k:list(v) for k,v in self._by_resource.items()}
            for rid,window,_ in req:
                if any(r.window.overlaps(window) for r in shadow.get(rid,())):
                    raise ConflictError(f"{rid} unavailable")
                placeholder=Reservation(-1,rid,window,None,self._generation)  # type: ignore[arg-type]
                shadow.setdefault(rid,[]).append(placeholder)
            out=[]
            for rid,window,payload in req:
                item=Reservation(self._next_token,rid,window,payload,self._generation+1)
                self._next_token += 1
                self._generation += 1
                self._by_resource.setdefault(rid,[]).append(item)
                self._by_resource[rid].sort(key=lambda r:(r.window.start,r.window.end,r.token))
                out.append(item)
            return tuple(out)
    def occupied_at(self,resource_id:str,instant:datetime)->Reservation[T]|None:
        t=utc(instant)
        with self._lock:
            for item in self._by_resource.get(resource_id,()):
                if item.window.contains(t):
                    return item
            return None
