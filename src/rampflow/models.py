from __future__ import annotations
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum
from typing import Mapping, FrozenSet, Iterable
from .timeutil import Window, utc

class FlightKind(str, Enum):
    ARRIVAL="arrival"
    DEPARTURE="departure"
    TURN="turn"

class ServiceState(str, Enum):
    PLANNED="planned"
    READY="ready"
    ACTIVE="active"
    BLOCKED="blocked"
    COMPLETE="complete"
    CANCELLED="cancelled"

@dataclass(frozen=True)
class Aircraft:
    registration: str
    family: str
    wingspan_m: float
    length_m: float
    mtow_kg: float
    seats: int
    door_height_m: float = 4.2
    gpu_voltage: int = 115
    pca_flow_kg_s: float = 1.4
    attributes: FrozenSet[str] = frozenset()
    def __post_init__(self):
        if not self.registration or self.wingspan_m <= 0 or self.length_m <= 0:
            raise ValueError("invalid aircraft")
        if self.mtow_kg <= 0 or self.seats < 0:
            raise ValueError("invalid aircraft capacity")

@dataclass(frozen=True)
class Flight:
    flight_id: str
    aircraft: Aircraft
    origin: str
    destination: str
    scheduled_on: datetime
    scheduled_off: datetime
    passengers: int
    bags: int
    kind: FlightKind = FlightKind.TURN
    international: bool = False
    airline: str = ""
    attributes: FrozenSet[str] = frozenset()
    def __post_init__(self):
        on=utc(self.scheduled_on); off=utc(self.scheduled_off)
        if off <= on:
            raise ValueError("off-block must follow on-block")
        if self.passengers < 0 or self.bags < 0:
            raise ValueError("negative load")
        object.__setattr__(self,"scheduled_on",on)
        object.__setattr__(self,"scheduled_off",off)
    @property
    def occupancy(self)->Window:
        return Window(self.scheduled_on,self.scheduled_off)

@dataclass(frozen=True)
class Resource:
    resource_id: str
    kind: str
    capabilities: FrozenSet[str] = frozenset()
    home: str = ""
    available: bool = True
    metadata: Mapping[str,str] = field(default_factory=dict)

@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    duration_min: int
    predecessors: FrozenSet[str] = frozenset()
    resource_kinds: tuple[str,...] = ()
    earliest_offset_min: int = 0
    latest_finish_offset_min: int | None = None
    interruptible: bool = False
    zone: str = ""
    metadata: Mapping[str,str] = field(default_factory=dict)
    def __post_init__(self):
        if self.duration_min <= 0:
            raise ValueError("positive task duration required")
        if self.task_id in self.predecessors:
            raise ValueError("self dependency")
        if self.latest_finish_offset_min is not None and self.latest_finish_offset_min < self.earliest_offset_min:
            raise ValueError("invalid task offsets")

@dataclass(frozen=True)
class TaskRun:
    task_id: str
    state: ServiceState
    planned: Window
    actual: Window | None = None
    resources: tuple[str,...] = ()
    blocked_reason: str | None = None
    version: int = 0
    def advance(self, state: ServiceState, actual: Window | None = None, blocked_reason: str | None = None) -> "TaskRun":
        allowed={
            ServiceState.PLANNED:{ServiceState.READY,ServiceState.CANCELLED,ServiceState.BLOCKED},
            ServiceState.READY:{ServiceState.ACTIVE,ServiceState.CANCELLED,ServiceState.BLOCKED},
            ServiceState.ACTIVE:{ServiceState.COMPLETE,ServiceState.BLOCKED},
            ServiceState.BLOCKED:{ServiceState.READY,ServiceState.CANCELLED},
            ServiceState.COMPLETE:set(),
            ServiceState.CANCELLED:set(),
        }
        if state not in allowed[self.state]:
            raise ValueError(f"invalid state transition {self.state}->{state}")
        return replace(self,state=state,actual=actual or self.actual,blocked_reason=blocked_reason,version=self.version+1)

@dataclass(frozen=True)
class Event:
    event_id: str
    occurred_at: datetime
    kind: str
    subject: str
    payload: Mapping[str,object] = field(default_factory=dict)
    revision: int = 0
    def __post_init__(self):
        object.__setattr__(self,"occurred_at",utc(self.occurred_at))

@dataclass(frozen=True)
class Decision:
    accepted: bool
    code: str
    reason: str
    details: Mapping[str,object] = field(default_factory=dict)

def index_resources(resources: Iterable[Resource]) -> dict[str,Resource]:
    out={}
    for r in resources:
        if r.resource_id in out:
            raise ValueError(f"duplicate resource {r.resource_id}")
        out[r.resource_id]=r
    return out
