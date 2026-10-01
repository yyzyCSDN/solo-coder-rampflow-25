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
        # Single monotonic token for both the rule set and the resource ledger.
        # A plan is only valid against the generation it was planned on.
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

    def _reservation_view(
        self,
        pending: Mapping[str, ServiceBooking] = (),
        *,
        replace_flight: str | None = None,
    ) -> tuple[ServiceBooking, ...]:
        """One reservation ledger shared by planning placement and commit.

        Committed bookings come first so a pending booking can never be judged
        against a half-written mutation of the planner state.  When a flight's
        plan is being revised, the bookings that revision replaces are left
        out of the view.
        """
        committed = (
            booking
            for plan in self.committed.values()
            if plan.flight_id != replace_flight
            for booking in plan.bookings.values()
        )
        return tuple(committed) + tuple(pending.values())

    def _reservations_clash(
        self,
        booking: ServiceBooking,
        rule: ServiceWindowRule,
        other: ServiceBooking,
    ) -> bool:
        """Same predicate for every feasibility decision.

        Two reservations clash when their windows overlap and either they take
        the same physical resource or they belong to one mutual-exclusion group
        (which also excludes them on different devices).
        """
        if not booking.window.overlaps(other.window):
            return False
        if (
            booking.resource_id is not None
            and booking.resource_id == other.resource_id
        ):
            return True
        other_rule = self.rules.get(other.service)
        if (
            rule.mutual_exclusion_group is not None
            and other_rule is not None
            and other_rule.mutual_exclusion_group == rule.mutual_exclusion_group
        ):
            return True
        return False

    def _blockers(
        self,
        service: str,
        resource_id: str | None,
        window: Window,
        pending: Mapping[str, ServiceBooking],
    ) -> list[Window]:
        rule = self.rules[service]
        probe = ServiceBooking(
            service, "", window, resource_id, self.rule_version, self.generation
        )
        return [
            other.window
            for other in self._reservation_view(pending)
            if self._reservations_clash(probe, rule, other)
        ]

    def _validate_plan(
        self, plan: WindowPlan, *, replace_flight: str | None = None
    ) -> None:
        """Validate the whole plan against one scheme: stale token, dependency
        order, window bounds, and the shared resource/mutual-exclusion ledger.

        Raises on the first violated invariant.  Never mutates planner state,
        so callers can apply all bookings only after this returns.
        """
        if plan.generation != self.generation:
            raise ServiceWindowError(
                "stale service plan: resource generation changed since planning"
            )
        if plan.departure <= plan.anchor:
            raise ServiceWindowError("invalid plan timeline")
        for booking in plan.bookings.values():
            if booking.flight_id != plan.flight_id:
                raise ServiceWindowError(
                    f"booking {booking.service} belongs to another flight"
                )
            rule = self.rules.get(booking.service)
            if rule is None:
                raise ServiceWindowError(
                    f"unknown service {booking.service}"
                )
            if booking.rule_version != self.rule_version:
                raise ServiceWindowError("service rules changed since planning")
            if booking.plan_generation != plan.generation:
                raise ServiceWindowError(
                    f"booking {booking.service} planned on another generation"
                )
        # Dependency order must hold inside the submitted plan itself.
        for name, booking in plan.bookings.items():
            rule = self.rules[name]
            for prerequisite in rule.prerequisites:
                prior = plan.bookings.get(prerequisite)
                if prior is None:
                    continue
                if booking.window.start < prior.window.end:
                    raise ServiceWindowError(
                        f"service {name} starts before prerequisite {prerequisite}"
                    )
        # Every hard service has to be present; soft misses must be exactly the
        # declared violations - no silently dropped or invented services.
        missing = sorted(
            name for name in self.rules if name not in plan.bookings
        )
        missing_hard = [name for name in missing if self.rules[name].hard]
        if missing_hard:
            raise ServiceWindowError(
                f"plan missing hard services: {', '.join(missing_hard)}"
            )
        if set(plan.soft_violations) != set(missing):
            raise ServiceWindowError(
                "declared soft violations do not match unplaced services"
            )
        for name, booking in plan.bookings.items():
            rule = self.rules[name]
            earliest = plan.anchor + timedelta(minutes=rule.earliest_offset_min)
            latest = min(
                plan.departure,
                plan.anchor + timedelta(minutes=rule.latest_finish_offset_min),
            )
            if booking.window.start < earliest or booking.window.end > latest:
                raise ServiceWindowError(
                    f"service {name} lies outside its feasible window"
                )
        # Resource reservations and mutual exclusion over one combined ledger,
        # including the plan's own bookings checked pairwise against each other
        # and against every committed booking.  Committed-vs-committed pairs are
        # already invariants, so only pairs touching the submitted plan count.
        view = self._reservation_view(
            plan.bookings, replace_flight=replace_flight
        )
        own = set(plan.bookings.values())
        for booking in view:
            rule = self.rules[booking.service]
            for other in view:
                if other is booking:
                    continue
                if booking not in own and other not in own:
                    continue
                if self._reservations_clash(booking, rule, other):
                    raise ServiceWindowError(
                        f"resource conflict for {booking.service}"
                    )

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
                placed = [
                    bookings[p].window.end
                    for p in rule.prerequisites
                    if p in bookings
                ]
                unplaced = [p for p in rule.prerequisites if p not in bookings]
                if placed:
                    earliest = max(earliest, max(placed))
                if unplaced:
                    # A prerequisite could not be placed (soft service).  The
                    # dependent service cannot be honestly placed either.
                    if rule.hard:
                        raise ServiceWindowError(
                            f"cannot place hard service {service}: "
                            f"prerequisite {sorted(unplaced)[0]} unplaced"
                        )
                    violations.append(service)
                    continue
            latest = min(
                departure,
                anchor + timedelta(minutes=rule.latest_finish_offset_min),
            )
            resource_options = resources_by_service.get(service, (None,))
            candidates = []
            for resource_id in resource_options:
                start = earliest
                window = None
                for _ in range(300):
                    candidate = Window(
                        start,
                        start + timedelta(minutes=rule.duration_min),
                    )
                    blockers = self._blockers(
                        service, resource_id, candidate, bookings
                    )
                    if not blockers:
                        window = candidate
                        break
                    start = max(blocker.end for blocker in blockers)
                if window is not None and window.end <= latest:
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
        plan = WindowPlan(
            flight_id,
            anchor,
            departure,
            bookings,
            self.generation,
            tuple(violations),
        )
        # The submitted plan must satisfy exactly the scheme commit enforces.
        self._validate_plan(plan)
        return plan

    def commit(self, plan: WindowPlan) -> None:
        existing = self.committed.get(plan.flight_id)
        if existing is not None and existing == plan:
            # Idempotent replay of the exact committed plan.
            return
        revising = existing is not None
        # Validate the entire plan before touching any reservation.  If the
        # plan is not wholly feasible, nothing is reserved - no "viable half".
        # A revision is judged with the flight's own prior bookings removed.
        self._validate_plan(plan, replace_flight=plan.flight_id)
        # Apply the whole plan atomically: first clear what the revision (if
        # any) replaces, then record every new booking, then publish the plan.
        if revising:
            for booking in existing.bookings.values():
                if booking.resource_id is None:
                    continue
                self.resource_bookings[booking.resource_id] = [
                    b
                    for b in self.resource_bookings.get(booking.resource_id, ())
                    if b.flight_id != plan.flight_id
                ]
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
        # The revision is planned against the committed ledger and reserves the
        # next generation up front, so a different commit landing first makes
        # this revision stale instead of silently double-booking resources.
        next_generation = self.generation + 1
        shifted = {}
        for service, booking in current.bookings.items():
            if service in preserve_started:
                shifted[service] = replace(
                    booking, plan_generation=next_generation
                )
                continue
            shifted[service] = replace(
                booking,
                window=booking.window.shift(minutes),
                plan_generation=next_generation,
            )
        updated = replace(
            current,
            departure=current.departure + timedelta(minutes=minutes),
            bookings=shifted,
            generation=next_generation,
            soft_violations=current.soft_violations,
        )
        # Judge the revision with the exact same scheme commit enforces:
        # window bounds, dependency order and the shared resource/mutex ledger,
        # with the flight's own prior bookings removed from the view.  If it is
        # not wholly feasible, release the reserved generation and reject.
        previous_generation = self.generation
        self.generation = next_generation
        try:
            self._validate_plan(updated, replace_flight=flight_id)
        except Exception:
            self.generation = previous_generation
            raise
        return updated

    def services_for(self, flight_id: str) -> tuple[ServiceBooking, ...]:
        plan = self.committed[flight_id]
        return tuple(
            sorted(
                plan.bookings.values(),
                key=lambda b: (b.window.start, b.service),
            )
        )
