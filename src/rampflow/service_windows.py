from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, Mapping

from .timeutil import Window, utc


@dataclass(frozen=True)
class ServiceWindowRule:
    service: str
    earliest_offset_min: int
    latest_finish_offset_min: int
    duration_min: int
    hard: bool = True
    mutual_exclusion_group: str | None = None
    prerequisites: frozenset[str] = frozenset()

    def __post_init__(self):
        if self.duration_min <= 0:
            raise ValueError("duration must be positive")
        if self.latest_finish_offset_min <= self.earliest_offset_min:
            raise ValueError("invalid service window")
        if self.service in self.prerequisites:
            raise ValueError("self prerequisite")


@dataclass(frozen=True)
class ServiceBooking:
    service: str
    flight_id: str
    window: Window
    resource_id: str | None
    rule_version: int
    plan_generation: int


@dataclass(frozen=True)
class WindowPlan:
    flight_id: str
    anchor: datetime
    departure: datetime
    bookings: Mapping[str, ServiceBooking]
    generation: int
    soft_violations: tuple[str, ...] = ()


class ServiceWindowError(RuntimeError):
    pass


class ServiceWindowPlanner:
    def __init__(self, rules: Iterable[ServiceWindowRule]):
        self.rules = {r.service: r for r in rules}
        if len(self.rules) == 0:
            raise ValueError("rules required")
        self.rule_version = 1
        self.generation = 0
        self.committed: dict[str, WindowPlan] = {}
        self.resource_bookings: dict[str, list[ServiceBooking]] = {}
        self._validate_graph()

    def _validate_graph(self) -> None:
        for name, rule in self.rules.items():
            missing = rule.prerequisites - set(self.rules)
            if missing:
                raise ValueError(f"unknown prerequisites for {name}: {sorted(missing)}")
        done = set()
        while len(done) < len(self.rules):
            ready = sorted(
                name
                for name, rule in self.rules.items()
                if name not in done and rule.prerequisites <= done
            )
            if not ready:
                raise ValueError("service rule dependency cycle")
            done.update(ready)

    def replace_rules(self, rules: Iterable[ServiceWindowRule]) -> None:
        next_rules = {r.service: r for r in rules}
        old = self.rules
        self.rules = next_rules
        try:
            self._validate_graph()
        except Exception:
            self.rules = old
            raise
        self.rule_version += 1
        self.generation += 1

    def _ordered_services(self) -> tuple[str, ...]:
        done = set()
        result = []
        while len(done) < len(self.rules):
            ready = sorted(
                name
                for name, rule in self.rules.items()
                if name not in done and rule.prerequisites <= done
            )
            if not ready:
                raise ServiceWindowError("service dependency cycle")
            result.extend(ready)
            done.update(ready)
        return tuple(result)

    def _resource_free(self, resource_id: str, window: Window) -> bool:
        return not any(
            booking.window.overlaps(window)
            for booking in self.resource_bookings.get(resource_id, ())
        )

    def _group_conflict(self, group: str | None, window: Window, bookings: Mapping[str, ServiceBooking]) -> bool:
        if group is None:
            return False
        for resource_bookings in self.resource_bookings.values():
            for booking in resource_bookings:
                other = self.rules.get(booking.service)
                if other is not None and other.mutual_exclusion_group == group and booking.window.overlaps(window):
                    return True
        return False

    def plan(
        self,
        flight_id: str,
        anchor: datetime,
        departure: datetime,
        resources_by_service: Mapping[str, tuple[str, ...]],
    ) -> WindowPlan:
        anchor = utc(anchor)
        departure = utc(departure)
        if departure <= anchor:
            raise ValueError("departure must follow anchor")
        bookings: dict[str, ServiceBooking] = {}
        violations: list[str] = []
        for service in self._ordered_services():
            rule = self.rules[service]
            earliest = anchor + timedelta(minutes=rule.earliest_offset_min)
            if rule.prerequisites:
                earliest = max(
                    earliest,
                    max(bookings[p].window.end for p in rule.prerequisites),
                )
            latest = min(
                departure,
                anchor + timedelta(minutes=rule.latest_finish_offset_min),
            )
            resource_options = resources_by_service.get(service, (None,))
            candidates = []
            for resource_id in resource_options:
                start = earliest
                for _ in range(300):
                    window = Window(
                        start,
                        start + timedelta(minutes=rule.duration_min),
                    )
                    blockers = []
                    if resource_id is not None:
                        blockers.extend(
                            b.window
                            for b in self.resource_bookings.get(resource_id, ())
                            if b.window.overlaps(window)
                        )
                    if self._group_conflict(
                        rule.mutual_exclusion_group,
                        window,
                        bookings,
                    ):
                        group_blockers = [
                            b.window
                            for name, b in bookings.items()
                            if self.rules[name].mutual_exclusion_group
                            == rule.mutual_exclusion_group
                            and b.window.overlaps(window)
                        ]
                        blockers.extend(group_blockers)
                    if not blockers:
                        break
                    start = max(b.end for b in blockers)
                else:
                    continue
                if window.end <= latest:
                    candidates.append(
                        (
                            window.end,
                            window.start,
                            "" if resource_id is None else resource_id,
                            resource_id,
                            window,
                        )
                    )
            if not candidates:
                if rule.hard:
                    raise ServiceWindowError(
                        f"cannot place hard service {service}"
                    )
                violations.append(service)
                continue
            _, _, _, resource_id, window = min(candidates)
            bookings[service] = ServiceBooking(
                service,
                flight_id,
                window,
                resource_id,
                self.rule_version,
                self.generation,
            )
        return WindowPlan(
            flight_id,
            anchor,
            departure,
            bookings,
            self.generation,
            tuple(violations),
        )

    def commit(self, plan: WindowPlan) -> None:
        if plan.flight_id in self.committed:
            if self.committed[plan.flight_id] == plan:
                return
            raise ServiceWindowError("flight already has committed service plan")
        for booking in plan.bookings.values():
            if booking.rule_version != self.rule_version:
                raise ServiceWindowError("service rules changed since planning")
            if booking.resource_id is not None and not self._resource_free(
                booking.resource_id,
                booking.window,
            ):
                raise ServiceWindowError(
                    f"resource conflict for {booking.service}"
                )
        for booking in plan.bookings.values():
            if booking.resource_id is not None:
                self.resource_bookings.setdefault(
                    booking.resource_id,
                    [],
                ).append(booking)
        self.committed[plan.flight_id] = plan
        self.generation += 1

    def release(self, flight_id: str) -> None:
        plan = self.committed.pop(flight_id, None)
        if plan is None:
            return
        for booking in plan.bookings.values():
            if booking.resource_id is None:
                continue
            self.resource_bookings[booking.resource_id] = [
                b
                for b in self.resource_bookings.get(booking.resource_id, ())
                if b.flight_id != flight_id
            ]
        self.generation += 1

    def delay(
        self,
        flight_id: str,
        minutes: int,
        preserve_started: frozenset[str] = frozenset(),
    ) -> WindowPlan:
        if minutes < 0:
            raise ValueError("delay must be non-negative")
        current = self.committed[flight_id]
        shifted = {}
        for service, booking in current.bookings.items():
            if service in preserve_started:
                shifted[service] = booking
                continue
            shifted[service] = replace(
                booking,
                window=booking.window.shift(minutes),
                plan_generation=self.generation,
            )
        updated = replace(
            current,
            departure=current.departure + timedelta(minutes=minutes),
            bookings=shifted,
            generation=self.generation,
        )
        for service, booking in shifted.items():
            rule = self.rules[service]
            latest = min(
                updated.departure,
                updated.anchor + timedelta(minutes=rule.latest_finish_offset_min),
            )
            if booking.window.end > latest and rule.hard:
                raise ServiceWindowError(
                    f"delay pushes {service} outside hard window"
                )
        return updated

    def services_for(self, flight_id: str) -> tuple[ServiceBooking, ...]:
        plan = self.committed[flight_id]
        return tuple(
            sorted(
                plan.bookings.values(),
                key=lambda b: (b.window.start, b.service),
            )
        )
