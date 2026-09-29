from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from heapq import heappush, heappop
from typing import Iterable, Mapping

from .timeutil import Window, utc


@dataclass(frozen=True)
class MobileResource:
    resource_id: str
    kind: str
    capabilities: frozenset[str]
    location: str
    available_at: datetime
    speed_m_s: float = 5.0
    active: bool = True

    def __post_init__(self):
        object.__setattr__(self, "available_at", utc(self.available_at))
        if self.speed_m_s <= 0:
            raise ValueError("speed must be positive")


@dataclass(frozen=True)
class DispatchRequest:
    request_id: str
    kind: str
    capability: str | None
    location: str
    earliest: datetime
    service_min: int
    latest_finish: datetime
    priority: int = 0

    def __post_init__(self):
        object.__setattr__(self, "earliest", utc(self.earliest))
        object.__setattr__(self, "latest_finish", utc(self.latest_finish))
        if self.latest_finish <= self.earliest:
            raise ValueError("invalid dispatch window")


@dataclass(frozen=True)
class Dispatch:
    request_id: str
    resource_id: str
    depart_at: datetime
    arrive_at: datetime
    service: Window
    from_location: str
    to_location: str
    generation: int


class DispatchError(RuntimeError):
    pass


class TravelGraph:
    def __init__(self):
        self.edges: dict[str, list[tuple[str, float]]] = {}

    def add(self, a: str, b: str, distance_m: float, bidirectional: bool = True) -> None:
        if distance_m < 0:
            raise ValueError("distance cannot be negative")
        self.edges.setdefault(a, []).append((b, distance_m))
        if bidirectional:
            self.edges.setdefault(b, []).append((a, distance_m))

    def distance(self, origin: str, destination: str) -> float:
        if origin == destination:
            return 0.0
        q = [(0.0, origin)]
        best = {origin: 0.0}
        while q:
            cost, node = heappop(q)
            if node == destination:
                return cost
            if cost != best.get(node):
                continue
            for nxt, weight in self.edges.get(node, ()):
                nc = cost + weight
                if nc < best.get(nxt, float("inf")):
                    best[nxt] = nc
                    heappush(q, (nc, nxt))
        raise DispatchError(f"no route {origin}->{destination}")


class ResourceDispatcher:
    def __init__(self, resources: Iterable[MobileResource], graph: TravelGraph):
        self.resources = {r.resource_id: r for r in resources}
        self.graph = graph
        self.dispatches: dict[str, Dispatch] = {}
        self.generation = 0

    def _candidate(self, resource: MobileResource, request: DispatchRequest) -> Dispatch | None:
        if not resource.active or resource.kind != request.kind:
            return None
        if request.capability and request.capability not in resource.capabilities:
            return None
        distance = self.graph.distance(resource.location, request.location)
        travel_seconds = int(distance / resource.speed_m_s)
        depart = max(resource.available_at, request.earliest - timedelta(seconds=travel_seconds))
        arrive = depart + timedelta(seconds=travel_seconds)
        start = max(arrive, request.earliest)
        service = Window(start, start + timedelta(minutes=request.service_min))
        if service.end > request.latest_finish:
            return None
        return Dispatch(
            request.request_id,
            resource.resource_id,
            depart,
            arrive,
            service,
            resource.location,
            request.location,
            self.generation,
        )

    def plan(self, request: DispatchRequest) -> Dispatch:
        candidates = []
        for resource in self.resources.values():
            dispatch = self._candidate(resource, request)
            if dispatch is None:
                continue
            idle = (dispatch.service.start - request.earliest).total_seconds()
            travel = (dispatch.arrive_at - dispatch.depart_at).total_seconds()
            candidates.append((idle, travel, dispatch.resource_id, dispatch))
        if not candidates:
            raise DispatchError(f"no resource for {request.request_id}")
        return min(candidates, key=lambda x: (x[0], x[1], x[2]))[3]

    def commit(self, dispatch: Dispatch) -> None:
        if dispatch.request_id in self.dispatches:
            existing = self.dispatches[dispatch.request_id]
            if existing == dispatch:
                return
            raise DispatchError("request already dispatched")
        resource = self.resources[dispatch.resource_id]
        if resource.location != dispatch.from_location:
            raise DispatchError("resource moved since plan")
        if resource.available_at > dispatch.depart_at:
            raise DispatchError("resource became busy")
        self.dispatches[dispatch.request_id] = dispatch
        self.resources[resource.resource_id] = replace(
            resource,
            location=dispatch.to_location,
            available_at=dispatch.service.end,
        )
        self.generation += 1

    def cancel(self, request_id: str, now: datetime) -> None:
        now = utc(now)
        dispatch = self.dispatches.get(request_id)
        if dispatch is None:
            return
        if now >= dispatch.depart_at:
            raise DispatchError("cannot automatically cancel after resource departure")
        resource = self.resources[dispatch.resource_id]
        self.resources[resource.resource_id] = replace(
            resource,
            location=dispatch.from_location,
            available_at=min(resource.available_at, dispatch.depart_at),
        )
        del self.dispatches[request_id]
        self.generation += 1

    def delay(self, request_id: str, minutes: int) -> Dispatch:
        if minutes < 0:
            raise ValueError("negative delay")
        old = self.dispatches[request_id]
        resource = self.resources[old.resource_id]
        shifted = Dispatch(
            old.request_id,
            old.resource_id,
            old.depart_at + timedelta(minutes=minutes),
            old.arrive_at + timedelta(minutes=minutes),
            old.service.shift(minutes),
            old.from_location,
            old.to_location,
            self.generation,
        )
        self.dispatches[request_id] = shifted
        self.resources[resource.resource_id] = replace(
            resource,
            available_at=shifted.service.end,
        )
        self.generation += 1
        return shifted

    def state(self, resource_id: str) -> MobileResource:
        return self.resources[resource_id]
