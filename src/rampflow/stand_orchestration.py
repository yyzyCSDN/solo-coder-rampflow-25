from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .timeutil import Window, utc


@dataclass(frozen=True)
class StandProfile:
    stand_id: str
    terminal: str
    max_wingspan_m: float
    international_capable: bool
    jetbridge_count: int
    tow_required_for_push: bool = False
    adjacent: frozenset[str] = frozenset()
    incompatible_simultaneous: frozenset[str] = frozenset()


@dataclass(frozen=True)
class StandDemand:
    flight_id: str
    window: Window
    wingspan_m: float
    international: bool
    terminal_preferences: tuple[str, ...] = ()
    needs_jetbridge: bool = True
    buffer_before_min: int = 5
    buffer_after_min: int = 10
    priority: int = 0


@dataclass(frozen=True)
class StandAssignment:
    flight_id: str
    stand_id: str
    window: Window
    protected_window: Window
    generation: int
    score: tuple[int, int, str]


@dataclass(frozen=True)
class StandMove:
    flight_id: str
    from_stand: str
    to_stand: str
    tow_window: Window
    reason: str


class StandConflict(RuntimeError):
    pass


class StandOrchestrator:
    def __init__(self, profiles: Iterable[StandProfile]):
        self.profiles = {p.stand_id: p for p in profiles}
        if not self.profiles:
            raise ValueError("at least one stand required")
        self.assignments: dict[str, StandAssignment] = {}
        self.by_stand: dict[str, list[StandAssignment]] = {
            sid: [] for sid in self.profiles
        }
        self.generation = 0

    def _protected(self, demand: StandDemand) -> Window:
        return Window(
            demand.window.start - timedelta(minutes=demand.buffer_before_min),
            demand.window.end + timedelta(minutes=demand.buffer_after_min),
        )

    def _profile_ok(self, p: StandProfile, d: StandDemand) -> bool:
        if d.wingspan_m > p.max_wingspan_m:
            return False
        if d.international and not p.international_capable:
            return False
        if d.needs_jetbridge and p.jetbridge_count < 1:
            return False
        return True

    def _adjacency_ok(self, stand_id: str, protected: Window, ignore: str | None = None) -> bool:
        p = self.profiles[stand_id]
        forbidden = set(p.incompatible_simultaneous)
        for other_id, op in self.profiles.items():
            if stand_id in op.incompatible_simultaneous:
                forbidden.add(other_id)
        for other_id in forbidden:
            for assignment in self.by_stand.get(other_id, ()):
                if assignment.flight_id == ignore:
                    continue
                if assignment.protected_window.overlaps(protected):
                    return False
        return True

    def _stand_free(self, stand_id: str, protected: Window, ignore: str | None = None) -> bool:
        for assignment in self.by_stand[stand_id]:
            if assignment.flight_id == ignore:
                continue
            if assignment.protected_window.overlaps(protected):
                return False
        return self._adjacency_ok(stand_id, protected, ignore)

    def candidates(self, demand: StandDemand) -> tuple[StandAssignment, ...]:
        protected = self._protected(demand)
        out = []
        preference_rank = {
            terminal: rank for rank, terminal in enumerate(demand.terminal_preferences)
        }
        for stand_id, p in self.profiles.items():
            if not self._profile_ok(p, demand):
                continue
            if not self._stand_free(stand_id, protected):
                continue
            rank = preference_rank.get(p.terminal, len(preference_rank) + 1)
            bridge_penalty = max(0, p.jetbridge_count - 1)
            score = (rank, bridge_penalty, stand_id)
            out.append(
                StandAssignment(
                    demand.flight_id,
                    stand_id,
                    demand.window,
                    protected,
                    self.generation,
                    score,
                )
            )
        return tuple(sorted(out, key=lambda a: a.score))

    def plan(self, demand: StandDemand) -> StandAssignment:
        options = self.candidates(demand)
        if not options:
            raise StandConflict(f"no feasible stand for {demand.flight_id}")
        return options[0]

    def commit(self, assignment: StandAssignment) -> None:
        if assignment.flight_id in self.assignments:
            existing = self.assignments[assignment.flight_id]
            if existing == assignment:
                return
            raise StandConflict("flight already has a stand")
        if assignment.stand_id not in self.profiles:
            raise StandConflict("unknown stand")
        if not self._stand_free(assignment.stand_id, assignment.protected_window):
            raise StandConflict("stand became unavailable")
        self.assignments[assignment.flight_id] = assignment
        self.by_stand[assignment.stand_id].append(assignment)
        self.by_stand[assignment.stand_id].sort(
            key=lambda a: (a.protected_window.start, a.flight_id)
        )
        self.generation += 1

    def release(self, flight_id: str) -> None:
        old = self.assignments.pop(flight_id, None)
        if old is None:
            return
        self.by_stand[old.stand_id] = [
            a for a in self.by_stand[old.stand_id] if a.flight_id != flight_id
        ]
        self.generation += 1

    def reassign(
        self,
        flight_id: str,
        new_stand: str,
        tow_start: datetime,
        tow_duration_min: int,
        reason: str,
    ) -> StandMove:
        current = self.assignments[flight_id]
        if new_stand == current.stand_id:
            raise ValueError("new stand equals current stand")
        if new_stand not in self.profiles:
            raise KeyError(new_stand)
        tow_start = utc(tow_start)
        tow_window = Window(
            tow_start,
            tow_start + timedelta(minutes=tow_duration_min),
        )
        if not current.window.contains(tow_start):
            raise StandConflict("tow must start while aircraft is on stand")
        remaining = Window(tow_window.end, current.window.end)
        protected = Window(
            remaining.start,
            current.protected_window.end,
        )
        if not self._stand_free(new_stand, protected, ignore=flight_id):
            raise StandConflict("destination stand unavailable after tow")
        old_stand = current.stand_id
        self.by_stand[old_stand] = [
            a for a in self.by_stand[old_stand] if a.flight_id != flight_id
        ]
        updated = replace(
            current,
            stand_id=new_stand,
            window=remaining,
            protected_window=protected,
            generation=self.generation,
        )
        self.assignments[flight_id] = updated
        self.by_stand[new_stand].append(updated)
        self.by_stand[new_stand].sort(
            key=lambda a: (a.protected_window.start, a.flight_id)
        )
        self.generation += 1
        return StandMove(flight_id, old_stand, new_stand, tow_window, reason)

    def occupancy(self, stand_id: str) -> tuple[StandAssignment, ...]:
        return tuple(self.by_stand[stand_id])

    def utilization(self, stand_id: str, window: Window) -> float:
        occupied = 0.0
        for assignment in self.by_stand[stand_id]:
            start = max(window.start, assignment.window.start)
            end = min(window.end, assignment.window.end)
            if start < end:
                occupied += (end - start).total_seconds()
        total = (window.end - window.start).total_seconds()
        return min(1.0, occupied / total) if total else 0.0
