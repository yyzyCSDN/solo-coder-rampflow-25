from __future__ import annotations
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Mapping
from .models import Aircraft, Flight, Decision
from .timeutil import Window
from .ledger import ReservationLedger, Reservation, ConflictError, StaleGeneration

@dataclass(frozen=True)
class Stand:
    stand_id: str
    max_wingspan_m: float
    max_length_m: float
    terminal: str
    remote: bool = False
    international_capable: bool = True
    bridge_count: int = 1
    lead_in_group: str = ""
    adjacency_group: str = ""
    pushback_lane: str = ""
    allowed_families: frozenset[str] = frozenset()
    prohibited_attributes: frozenset[str] = frozenset()
    service_attributes: frozenset[str] = frozenset()
    tow_in_buffer_min: int = 5
    tow_out_buffer_min: int = 8
    def fits(self, aircraft: Aircraft) -> bool:
        if aircraft.wingspan_m > self.max_wingspan_m or aircraft.length_m > self.max_length_m:
            return False
        if self.allowed_families and aircraft.family not in self.allowed_families:
            return False
        if self.prohibited_attributes.intersection(aircraft.attributes):
            return False
        return True

@dataclass(frozen=True)
class StandUse:
    flight_id: str
    aircraft_reg: str
    actual_window: Window
    protected_window: Window
    tow_in: bool
    tow_out: bool
    international: bool

@dataclass(frozen=True)
class StandAssignment:
    stand_id: str
    score: float
    reasons: tuple[str,...]
    reservation: Reservation[StandUse] | None = None

@dataclass(frozen=True)
class AdjacencyRule:
    group: str
    max_combined_wingspan_m: float
    simultaneous_widebody_limit: int = 1
    widebody_threshold_m: float = 52.0

class StandPlanner:
    def __init__(self, stands: Iterable[Stand], adjacency_rules: Iterable[AdjacencyRule] = ()):
        self.stands={s.stand_id:s for s in stands}
        if not self.stands:
            raise ValueError("at least one stand required")
        if len(self.stands) != len(list(stands)) if not isinstance(stands, (list,tuple)) else False:
            raise ValueError("duplicate stand")
        self.rules={r.group:r for r in adjacency_rules}
        self.ledger: ReservationLedger[StandUse]=ReservationLedger()

    def _protected(self, stand: Stand, occupancy: Window, tow_in: bool, tow_out: bool) -> Window:
        before=stand.tow_in_buffer_min if tow_in else 0
        after=stand.tow_out_buffer_min if tow_out else 0
        return occupancy.expand(before=before, after=after)

    def _lane_conflict(self, stand: Stand, protected: Window) -> list[str]:
        if not stand.pushback_lane:
            return []
        hit=[]
        for sid, candidate in self.stands.items():
            if sid == stand.stand_id or candidate.pushback_lane != stand.pushback_lane:
                continue
            for r in self.ledger.reservations(sid):
                if r.payload.protected_window.overlaps(protected):
                    hit.append(sid)
        return hit

    def _adjacency_conflict(self, stand: Stand, aircraft: Aircraft, protected: Window) -> list[str]:
        group=stand.adjacency_group
        if not group or group not in self.rules:
            return []
        rule=self.rules[group]
        overlapping=[]
        widths=[aircraft.wingspan_m]
        wide=1 if aircraft.wingspan_m >= rule.widebody_threshold_m else 0
        for sid,candidate in self.stands.items():
            if sid==stand.stand_id or candidate.adjacency_group != group:
                continue
            for r in self.ledger.reservations(sid):
                if r.payload.protected_window.overlaps(protected):
                    overlapping.append(sid)
                    other_reg=r.payload.aircraft_reg
                    other_width=float(r.payload.actual_window.minutes*0 + 0)
                    widths.append(self._wingspan_for_registration(other_reg))
                    if widths[-1] >= rule.widebody_threshold_m:
                        wide += 1
        if wide > rule.simultaneous_widebody_limit:
            return overlapping or ["widebody-limit"]
        if sum(widths) > rule.max_combined_wingspan_m:
            return overlapping or ["combined-wingspan"]
        return []

    def _wingspan_for_registration(self, registration: str) -> float:
        for items in self.ledger._by_resource.values():
            for r in items:
                if r.payload.aircraft_reg == registration:
                    metadata=getattr(r.payload, "_aircraft_wingspan_m", None)
                    if metadata is not None:
                        return float(metadata)
        return 36.0

    def evaluate(self, flight: Flight, stand_id: str, tow_in: bool = False, tow_out: bool = False) -> Decision:
        if stand_id not in self.stands:
            return Decision(False,"unknown-stand","stand does not exist")
        stand=self.stands[stand_id]
        if not stand.fits(flight.aircraft):
            return Decision(False,"geometry","aircraft is not compatible with stand")
        if flight.international and not stand.international_capable:
            return Decision(False,"border-control","international flight requires capable stand")
        protected=self._protected(stand,flight.occupancy,tow_in,tow_out)
        direct=self.ledger.conflicts(stand_id,protected)
        if direct:
            return Decision(False,"occupied","stand occupancy overlaps protected interval",{"tokens":[r.token for r in direct]})
        lane=self._lane_conflict(stand,protected)
        if lane:
            return Decision(False,"pushback-lane","shared pushback lane conflict",{"stands":lane})
        adjacent=self._adjacency_conflict(stand,flight.aircraft,protected)
        if adjacent:
            return Decision(False,"adjacency","adjacent stand safety envelope conflict",{"stands":adjacent})
        return Decision(True,"ok","stand feasible",{"protected_start":protected.start,"protected_end":protected.end})

    def rank(self, flight: Flight, preferred_terminal: str | None = None, tow_in: bool = False, tow_out: bool = False) -> list[StandAssignment]:
        out=[]
        for stand in self.stands.values():
            d=self.evaluate(flight,stand.stand_id,tow_in,tow_out)
            if not d.accepted:
                continue
            score=100.0
            reasons=[]
            if preferred_terminal and stand.terminal == preferred_terminal:
                score += 25.0; reasons.append("preferred-terminal")
            if not stand.remote:
                score += 18.0; reasons.append("contact")
            if stand.bridge_count >= 2 and flight.aircraft.seats >= 250:
                score += 8.0; reasons.append("dual-bridge")
            geometry_margin=(stand.max_wingspan_m-flight.aircraft.wingspan_m)+(stand.max_length_m-flight.aircraft.length_m)/2
            score += max(0.0,min(12.0,geometry_margin/5))
            if "needs-high-loader" in flight.attributes and "high-loader" in stand.service_attributes:
                score += 7.0; reasons.append("service-fit")
            out.append(StandAssignment(stand.stand_id,score,tuple(reasons)))
        return sorted(out,key=lambda a:(-a.score,a.stand_id))

    def assign(self, flight: Flight, stand_id: str, tow_in: bool = False, tow_out: bool = False, expected_generation: int | None = None) -> StandAssignment:
        decision=self.evaluate(flight,stand_id,tow_in,tow_out)
        if not decision.accepted:
            raise ConflictError(decision.reason)
        stand=self.stands[stand_id]
        protected=self._protected(stand,flight.occupancy,tow_in,tow_out)
        use=StandUse(flight.flight_id,flight.aircraft.registration,flight.occupancy,protected,tow_in,tow_out,flight.international)
        object.__setattr__(use,"_aircraft_wingspan_m",flight.aircraft.wingspan_m)
        reservation=self.ledger.reserve(stand_id,protected,use,expected_generation)
        ranked={x.stand_id:x for x in self.rank(flight,None,tow_in,tow_out)}
        base=ranked.get(stand_id,StandAssignment(stand_id,0.0,()))
        return StandAssignment(stand_id,base.score,base.reasons,reservation)

    def move(self, token: int, new_stand_id: str, expected_generation: int) -> Reservation[StandUse]:
        generation, rows=self.ledger.snapshot()
        if generation != expected_generation:
            raise StaleGeneration("stand plan changed")
        current=next((r for r in rows if r.token==token),None)
        if current is None:
            raise KeyError(token)
        if new_stand_id not in self.stands:
            raise KeyError(new_stand_id)
        use=current.payload
        fake_aircraft=Aircraft(use.aircraft_reg,"UNKNOWN",self._wingspan_for_registration(use.aircraft_reg),20,1,1)
        fake_flight=Flight(use.flight_id,fake_aircraft,"","",use.actual_window.start,use.actual_window.end,0,0,international=use.international)
        decision=self.evaluate(fake_flight,new_stand_id,use.tow_in,use.tow_out)
        if not decision.accepted:
            raise ConflictError(decision.reason)
        self.ledger.release(token,expected_generation)
        stand=self.stands[new_stand_id]
        protected=self._protected(stand,use.actual_window,use.tow_in,use.tow_out)
        moved=StandUse(use.flight_id,use.aircraft_reg,use.actual_window,protected,use.tow_in,use.tow_out,use.international)
        object.__setattr__(moved,"_aircraft_wingspan_m",self._wingspan_for_registration(use.aircraft_reg))
        return self.ledger.reserve(new_stand_id,protected,moved,self.ledger.generation)

    def utilization(self, horizon: Window) -> Mapping[str,float]:
        result={}
        for sid in self.stands:
            covered=0
            for item in self.ledger.reservations(sid):
                inter=item.window.intersection(horizon)
                if inter:
                    covered += inter.minutes
            result[sid]=min(1.0,covered/horizon.minutes)
        return result
