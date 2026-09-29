from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import product
from typing import Iterable, Mapping

from .timeutil import Window, utc


@dataclass(frozen=True)
class FlightRecoveryInput:
    flight_id: str
    scheduled_off: datetime
    passengers: int
    connections: int
    curfew: datetime | None
    minimum_turn_min: int
    current_ready: datetime
    cancellation_cost: int

    def __post_init__(self):
        object.__setattr__(self, "scheduled_off", utc(self.scheduled_off))
        object.__setattr__(self, "current_ready", utc(self.current_ready))
        if self.curfew is not None:
            object.__setattr__(self, "curfew", utc(self.curfew))


@dataclass(frozen=True)
class RecoveryOption:
    flight_id: str
    action: str
    departure: datetime | None
    stand: str | None
    score: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RecoveryPlan:
    options: Mapping[str, RecoveryOption]
    total_score: int
    generated_at: datetime


class DisruptionOptimizer:
    def __init__(
        self,
        delay_cost_per_minute: int = 2,
        connection_cost: int = 80,
        passenger_cost: int = 1,
    ):
        self.delay_cost_per_minute = delay_cost_per_minute
        self.connection_cost = connection_cost
        self.passenger_cost = passenger_cost

    def options_for(
        self,
        flight: FlightRecoveryInput,
        candidate_stands: Iterable[str],
        stand_windows: Mapping[str, tuple[Window, ...]],
        delay_steps: Iterable[int] = (0, 10, 20, 30, 45, 60, 90),
    ) -> tuple[RecoveryOption, ...]:
        out = []
        for delay in delay_steps:
            departure = max(
                flight.scheduled_off + timedelta(minutes=delay),
                flight.current_ready,
            )
            if flight.curfew is not None and departure > flight.curfew:
                continue
            delay_min = max(
                0,
                int((departure - flight.scheduled_off).total_seconds() // 60),
            )
            for stand in candidate_stands:
                blocked = any(w.contains(departure) for w in stand_windows.get(stand, ()))
                if blocked:
                    continue
                score = (
                    delay_min * self.delay_cost_per_minute
                    + min(flight.connections, delay_min // 10) * self.connection_cost
                    + (flight.passengers * self.passenger_cost if delay_min > 60 else 0)
                )
                reasons = []
                if departure == flight.current_ready and flight.current_ready > flight.scheduled_off:
                    reasons.append("ready-time-limited")
                if delay_min:
                    reasons.append(f"delay:{delay_min}")
                out.append(
                    RecoveryOption(
                        flight.flight_id,
                        "operate",
                        departure,
                        stand,
                        score,
                        tuple(reasons),
                    )
                )
        out.append(
            RecoveryOption(
                flight.flight_id,
                "cancel",
                None,
                None,
                flight.cancellation_cost,
                ("cancellation",),
            )
        )
        return tuple(sorted(out, key=lambda o: (o.score, o.action, o.departure or datetime.max.replace(tzinfo=flight.scheduled_off.tzinfo))))

    def optimize(
        self,
        flights: Iterable[FlightRecoveryInput],
        candidate_stands: Mapping[str, tuple[str, ...]],
        stand_windows: Mapping[str, tuple[Window, ...]],
        generated_at: datetime,
    ) -> RecoveryPlan:
        flights = tuple(flights)
        option_sets = [
            self.options_for(f, candidate_stands.get(f.flight_id, ()), stand_windows)
            for f in flights
        ]
        best = None
        for combo in product(*[opts[:8] for opts in option_sets]):
            used: dict[str, list[datetime]] = {}
            conflict = False
            for option in combo:
                if option.action == "cancel":
                    continue
                assert option.departure is not None and option.stand is not None
                for existing in used.setdefault(option.stand, []):
                    if abs((existing - option.departure).total_seconds()) < 15 * 60:
                        conflict = True
                        break
                if conflict:
                    break
                used[option.stand].append(option.departure)
            if conflict:
                continue
            score = sum(o.score for o in combo)
            tie = tuple((o.flight_id, o.action, o.departure or generated_at, o.stand or "") for o in combo)
            candidate = (score, tie, combo)
            if best is None or candidate[:2] < best[:2]:
                best = candidate
        if best is None:
            raise RuntimeError("no disruption recovery plan")
        combo = best[2]
        return RecoveryPlan(
            {o.flight_id: o for o in combo},
            best[0],
            utc(generated_at),
        )
