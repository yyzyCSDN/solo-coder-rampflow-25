from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Generic, Iterable, Mapping, TypeVar
from .timeutil import utc

T=TypeVar("T")

class VersionConflict(RuntimeError):
    pass

class MissingEntity(KeyError):
    pass

@dataclass(frozen=True)
class Versioned(Generic[T]):
    key:str
    version:int
    updated_at:datetime
    value:T

@dataclass(frozen=True)
class Change(Generic[T]):
    sequence:int
    key:str
    old:Versioned[T]|None
    new:Versioned[T]|None
    actor:str
    reason:str
    occurred_at:datetime

@dataclass(frozen=True)
class Snapshot(Generic[T]):
    generation:int
    records:Mapping[str,Versioned[T]]

class VersionedStore(Generic[T]):
    """Small in-memory repository with optimistic concurrency and immutable snapshots.

    The store models the semantics used by the operational services: readers can hold a
    generation-stamped snapshot while writers perform compare-and-swap updates.  Batch
    writes are validated first and committed atomically under one lock.
    """
    def __init__(self):
        self._records:dict[str,Versioned[T]]={}
        self._generation=0
        self._sequence=0
        self._changes:list[Change[T]]=[]
        self._lock=RLock()

    @property
    def generation(self)->int:
        with self._lock:return self._generation

    def get(self,key:str)->Versioned[T]:
        with self._lock:
            try:return self._records[key]
            except KeyError:raise MissingEntity(key) from None

    def find(self,key:str)->Versioned[T]|None:
        with self._lock:return self._records.get(key)

    def list(self)->tuple[Versioned[T],...]:
        with self._lock:return tuple(self._records[k] for k in sorted(self._records))

    def snapshot(self)->Snapshot[T]:
        with self._lock:return Snapshot(self._generation,dict(self._records))

    def create(self,key:str,value:T,actor:str,reason:str,at:datetime)->Versioned[T]:
        if not key:raise ValueError("key required")
        at=utc(at)
        with self._lock:
            if key in self._records:raise VersionConflict(f"{key} already exists")
            rec=Versioned(key,1,at,value)
            self._commit_change(key,None,rec,actor,reason,at)
            return rec

    def put(self,key:str,value:T,expected_version:int,actor:str,reason:str,at:datetime)->Versioned[T]:
        at=utc(at)
        with self._lock:
            old=self._records.get(key)
            if old is None:raise MissingEntity(key)
            if old.version!=expected_version:
                raise VersionConflict(f"{key} expected {expected_version}, found {old.version}")
            rec=Versioned(key,old.version+1,at,value)
            self._commit_change(key,old,rec,actor,reason,at)
            return rec

    def delete(self,key:str,expected_version:int,actor:str,reason:str,at:datetime)->None:
        at=utc(at)
        with self._lock:
            old=self._records.get(key)
            if old is None:raise MissingEntity(key)
            if old.version!=expected_version:
                raise VersionConflict(f"{key} expected {expected_version}, found {old.version}")
            self._commit_change(key,old,None,actor,reason,at)

    def batch(self,ops:Iterable[tuple[str,str,T|None,int|None]],actor:str,reason:str,at:datetime)->tuple[Versioned[T]|None,...]:
        at=utc(at); operations=tuple(ops)
        with self._lock:
            staged=dict(self._records); out=[]
            for action,key,value,expected in operations:
                old=staged.get(key)
                if action=="create":
                    if old is not None:raise VersionConflict(f"{key} already exists")
                    rec=Versioned(key,1,at,value) # type: ignore[arg-type]
                    staged[key]=rec;out.append(rec)
                elif action=="put":
                    if old is None:raise MissingEntity(key)
                    if expected!=old.version:raise VersionConflict(f"{key} stale")
                    rec=Versioned(key,old.version+1,at,value) # type: ignore[arg-type]
                    staged[key]=rec;out.append(rec)
                elif action=="delete":
                    if old is None:raise MissingEntity(key)
                    if expected!=old.version:raise VersionConflict(f"{key} stale")
                    del staged[key];out.append(None)
                else:raise ValueError(f"unknown operation {action}")
            for (_,key,_,_),new in zip(operations,out):
                old=self._records.get(key)
                self._commit_change(key,old,new,actor,reason,at,bump_generation=False)
            if operations:self._generation+=1
            return tuple(out)

    def changes(self,after_sequence:int=0)->tuple[Change[T],...]:
        with self._lock:return tuple(c for c in self._changes if c.sequence>after_sequence)

    def restore_snapshot(self,snapshot:Snapshot[T],expected_generation:int)->None:
        with self._lock:
            if self._generation!=expected_generation:raise VersionConflict("store changed since restore was planned")
            now=max((r.updated_at for r in snapshot.records.values()),default=datetime.now().astimezone())
            old=dict(self._records);new=dict(snapshot.records)
            for key in sorted(set(old)|set(new)):
                if old.get(key)!=new.get(key):self._commit_change(key,old.get(key),new.get(key),"system","snapshot restore",now,bump_generation=False)
            self._records=new;self._generation+=1

    def _commit_change(self,key:str,old:Versioned[T]|None,new:Versioned[T]|None,actor:str,reason:str,at:datetime,bump_generation:bool=True)->None:
        self._sequence+=1
        if new is None:self._records.pop(key,None)
        else:self._records[key]=new
        self._changes.append(Change(self._sequence,key,old,new,actor,reason,at))
        if bump_generation:self._generation+=1
