from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Mapping

from .timeutil import utc


class ConnectionState(str, Enum):
    PLANNED = "planned"
    AT_RISK = "at_risk"
    PROTECTED = "protected"
    MISCONNECTED = "misconnected"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Connection:
    passenger_id: str
    inbound_flight: str
    outbound_flight: str
    inbound_onblock: datetime
    outbound_offblock: datetime
    minimum_connect_min: int
    assistance_min: int = 0
    same_terminal: bool = True
    protected_until: datetime | None = None
    state: ConnectionState = ConnectionState.PLANNED
    version: int = 1

    def __post_init__(self):
        object.__setattr__(self, "inbound_onblock", utc(self.inbound_onblock))
        object.__setattr__(self, "outbound_offblock", utc(self.outbound_offblock))
        if self.protected_until is not None:
            object.__setattr__(self, "protected_until", utc(self.protected_until))
        if self.outbound_offblock <= self.inbound_onblock:
            raise ValueError("outbound must follow inbound")


@dataclass(frozen=True)
class RebookingOption:
    flight_id: str
    departure: datetime
    seats: int
    arrival: datetime
    terminal_change_min: int = 0

    def __post_init__(self):
        object.__setattr__(self, "departure", utc(self.departure))
        object.__setattr__(self, "arrival", utc(self.arrival))
        if self.arrival <= self.departure:
            raise ValueError("arrival must follow departure")


class ConnectionManager:
    def __init__(self, terminal_change_min: int = 20):
        self.terminal_change_min = terminal_change_min
        self.connections: dict[str, Connection] = {}
        self.flight_departures: dict[str, datetime] = {}
        self.flight_arrivals: dict[str, datetime] = {}

    def _key(self, passenger_id: str, outbound_flight: str) -> str:
        return f"{passenger_id}:{outbound_flight}"

    def register(self, connection: Connection) -> None:
        key = self._key(connection.passenger_id, connection.outbound_flight)
        if key in self.connections:
            raise ValueError("duplicate connection")
        self.connections[key] = connection

    def effective_minimum(self, connection: Connection) -> int:
        extra = 0 if connection.same_terminal else self.terminal_change_min
        return connection.minimum_connect_min + connection.assistance_min + extra

    def margin_minutes(self, connection: Connection) -> int:
        inbound = self.flight_arrivals.get(
            connection.inbound_flight,
            connection.inbound_onblock,
        )
        outbound = self.flight_departures.get(
            connection.outbound_flight,
            connection.outbound_offblock,
        )
        raw = int((outbound - inbound).total_seconds() // 60)
        return raw - self.effective_minimum(connection)

    def evaluate(self, connection: Connection) -> Connection:
        margin = self.margin_minutes(connection)
        outbound = self.flight_departures.get(
            connection.outbound_flight,
            connection.outbound_offblock,
        )
        protected = connection.protected_until
        if margin >= 0:
            state = ConnectionState.COMPLETED if connection.state == ConnectionState.COMPLETED else ConnectionState.PLANNED
        elif protected is not None and outbound <= protected:
            state = ConnectionState.PROTECTED
        else:
            state = ConnectionState.AT_RISK
        return replace(connection, state=state, version=connection.version + 1)

    def update_flight(
        self,
        flight_id: str,
        *,
        onblock: datetime | None = None,
        offblock: datetime | None = None,
    ) -> tuple[Connection, ...]:
        if onblock is not None:
            self.flight_arrivals[flight_id] = utc(onblock)
        if offblock is not None:
            self.flight_departures[flight_id] = utc(offblock)
        changed = []
        for key, connection in tuple(self.connections.items()):
            if flight_id not in {connection.inbound_flight, connection.outbound_flight}:
                continue
            updated = self.evaluate(connection)
            self.connections[key] = updated
            changed.append(updated)
        return tuple(changed)

    def protect(self, passenger_id: str, outbound_flight: str, until: datetime) -> Connection:
        key = self._key(passenger_id, outbound_flight)
        current = self.connections[key]
        until = utc(until)
        if until < current.outbound_offblock:
            raise ValueError("protection must cover scheduled departure")
        updated = replace(
            current,
            protected_until=until,
            state=ConnectionState.PROTECTED,
            version=current.version + 1,
        )
        self.connections[key] = updated
        return updated

    def mark_completed(self, passenger_id: str, outbound_flight: str) -> Connection:
        key = self._key(passenger_id, outbound_flight)
        current = self.connections[key]
        updated = replace(
            current,
            state=ConnectionState.COMPLETED,
            version=current.version + 1,
        )
        self.connections[key] = updated
        return updated

    def rebooking_options(
        self,
        passenger_id: str,
        outbound_flight: str,
        options: Iterable[RebookingOption],
        destination_arrival_limit: datetime | None = None,
    ) -> tuple[RebookingOption, ...]:
        key = self._key(passenger_id, outbound_flight)
        connection = self.connections[key]
        inbound = self.flight_arrivals.get(
            connection.inbound_flight,
            connection.inbound_onblock,
        )
        limit = utc(destination_arrival_limit) if destination_arrival_limit else None
        feasible = []
        for option in options:
            if option.seats <= 0:
                continue
            earliest = inbound + timedelta(
                minutes=connection.minimum_connect_min
                + connection.assistance_min
                + option.terminal_change_min
            )
            if option.departure < earliest:
                continue
            if limit is not None and option.arrival > limit:
                continue
            feasible.append(option)
        return tuple(sorted(feasible, key=lambda o: (o.arrival, o.departure, o.flight_id)))

    def apply_rebooking(
        self,
        passenger_id: str,
        old_outbound: str,
        option: RebookingOption,
    ) -> Connection:
        key = self._key(passenger_id, old_outbound)
        old = self.connections.pop(key)
        updated = replace(
            old,
            outbound_flight=option.flight_id,
            outbound_offblock=option.departure,
            protected_until=None,
            state=ConnectionState.PLANNED,
            version=old.version + 1,
        )
        new_key = self._key(passenger_id, option.flight_id)
        if new_key in self.connections:
            self.connections[key] = old
            raise ValueError("target rebooking already exists")
        self.connections[new_key] = updated
        return updated

    def risk_summary(self) -> Mapping[str, int]:
        counts = {state.value: 0 for state in ConnectionState}
        for connection in self.connections.values():
            counts[connection.state.value] += 1
        return counts
