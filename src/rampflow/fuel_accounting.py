from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping

from .timeutil import utc


Q = Decimal("0.001")
MONEY = Decimal("0.01")


@dataclass(frozen=True)
class FuelTank:
    tank_id: str
    capacity_l: Decimal
    usable_l: Decimal
    density_kg_l: Decimal


@dataclass(frozen=True)
class FuelDelivery:
    delivery_id: str
    flight_id: str
    tank_id: str
    meter_start_l: Decimal
    meter_end_l: Decimal
    temperature_c: Decimal
    density_kg_l: Decimal
    delivered_at: datetime
    corrected_l: Decimal
    mass_kg: Decimal
    price_per_l: Decimal
    amount: Decimal
    reversed_by: str | None = None


class FuelAccountingError(RuntimeError):
    pass


def quant(value: Decimal, unit: Decimal = Q) -> Decimal:
    return value.quantize(unit, rounding=ROUND_HALF_UP)


class FuelLedger:
    def __init__(self, tanks: Iterable[FuelTank], reference_temp_c: Decimal = Decimal("15")):
        self.tanks = {t.tank_id: t for t in tanks}
        self.reference_temp_c = reference_temp_c
        self.deliveries: dict[str, FuelDelivery] = {}
        self.inventory_l: dict[str, Decimal] = {
            t.tank_id: quant(t.usable_l) for t in tanks
        }
        self.flight_totals_l: dict[str, Decimal] = {}
        self.flight_totals_kg: dict[str, Decimal] = {}

    def _temperature_corrected(
        self,
        volume_l: Decimal,
        temp_c: Decimal,
        coefficient: Decimal = Decimal("0.00095"),
    ) -> Decimal:
        factor = Decimal("1") + coefficient * (temp_c - self.reference_temp_c)
        if factor <= 0:
            raise FuelAccountingError("invalid thermal correction")
        return quant(volume_l / factor)

    def record(
        self,
        delivery_id: str,
        flight_id: str,
        tank_id: str,
        meter_start_l: Decimal,
        meter_end_l: Decimal,
        temperature_c: Decimal,
        measured_density_kg_l: Decimal,
        price_per_l: Decimal,
        delivered_at: datetime,
    ) -> FuelDelivery:
        if delivery_id in self.deliveries:
            return self.deliveries[delivery_id]
        if tank_id not in self.tanks:
            raise KeyError(tank_id)
        raw = meter_end_l - meter_start_l
        if raw <= 0:
            raise FuelAccountingError("fuel meter must advance")
        corrected = self._temperature_corrected(raw, temperature_c)
        if corrected > self.inventory_l[tank_id]:
            raise FuelAccountingError("insufficient tank inventory")
        mass = quant(corrected * measured_density_kg_l)
        amount = quant(corrected * price_per_l, MONEY)
        delivery = FuelDelivery(
            delivery_id,
            flight_id,
            tank_id,
            meter_start_l,
            meter_end_l,
            temperature_c,
            measured_density_kg_l,
            utc(delivered_at),
            corrected,
            mass,
            price_per_l,
            amount,
        )
        self.inventory_l[tank_id] = quant(self.inventory_l[tank_id] - corrected)
        self.flight_totals_l[flight_id] = quant(
            self.flight_totals_l.get(flight_id, Decimal("0")) + corrected
        )
        self.flight_totals_kg[flight_id] = quant(
            self.flight_totals_kg.get(flight_id, Decimal("0")) + mass
        )
        self.deliveries[delivery_id] = delivery
        return delivery

    def reverse(
        self,
        delivery_id: str,
        reversal_id: str,
        reversed_at: datetime,
    ) -> FuelDelivery:
        original = self.deliveries[delivery_id]
        if original.reversed_by is not None:
            raise FuelAccountingError("delivery already reversed")
        if reversal_id in self.deliveries:
            raise FuelAccountingError("duplicate reversal id")
        reversal = FuelDelivery(
            reversal_id,
            original.flight_id,
            original.tank_id,
            original.meter_end_l,
            original.meter_start_l,
            original.temperature_c,
            original.density_kg_l,
            utc(reversed_at),
            -original.corrected_l,
            -original.mass_kg,
            original.price_per_l,
            -original.amount,
        )
        self.deliveries[delivery_id] = replace(original, reversed_by=reversal_id)
        self.deliveries[reversal_id] = reversal
        self.inventory_l[original.tank_id] = quant(
            self.inventory_l[original.tank_id] + original.corrected_l
        )
        self.flight_totals_l[original.flight_id] = quant(
            self.flight_totals_l[original.flight_id] - original.corrected_l
        )
        self.flight_totals_kg[original.flight_id] = quant(
            self.flight_totals_kg[original.flight_id] - original.mass_kg
        )
        return reversal

    def replenish(self, tank_id: str, amount_l: Decimal) -> Decimal:
        if amount_l <= 0:
            raise FuelAccountingError("replenishment must be positive")
        tank = self.tanks[tank_id]
        next_level = quant(self.inventory_l[tank_id] + amount_l)
        if next_level > tank.capacity_l:
            raise FuelAccountingError("tank capacity exceeded")
        self.inventory_l[tank_id] = next_level
        return next_level

    def flight_summary(self, flight_id: str) -> Mapping[str, Decimal]:
        amount = sum(
            d.amount
            for d in self.deliveries.values()
            if d.flight_id == flight_id
        )
        return {
            "volume_l": self.flight_totals_l.get(flight_id, Decimal("0")),
            "mass_kg": self.flight_totals_kg.get(flight_id, Decimal("0")),
            "amount": quant(amount, MONEY),
        }

    def inventory_mass_kg(self, tank_id: str) -> Decimal:
        tank = self.tanks[tank_id]
        return quant(self.inventory_l[tank_id] * tank.density_kg_l)
