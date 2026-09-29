from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from enum import Enum
from typing import Iterable, Mapping

from .timeutil import utc


class Effect(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE = "require"


@dataclass(frozen=True)
class PolicyContext:
    operation: str
    flight_id: str
    airport: str
    terminal: str
    at: datetime
    attributes: Mapping[str, object]

    def __post_init__(self):
        object.__setattr__(self, "at", utc(self.at))


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    priority: int
    operations: frozenset[str]
    effect: Effect
    reason: str
    required_attribute: str | None = None
    required_value: object | None = None
    terminal: str | None = None
    airport: str | None = None
    start_local: time | None = None
    end_local: time | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None

    def __post_init__(self):
        if self.valid_from is not None:
            object.__setattr__(self, "valid_from", utc(self.valid_from))
        if self.valid_until is not None:
            object.__setattr__(self, "valid_until", utc(self.valid_until))
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("invalid policy validity")


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    matched_rules: tuple[str, ...]
    requirements: tuple[str, ...]
    reasons: tuple[str, ...]


class PolicyEngine:
    def __init__(self, rules: Iterable[PolicyRule] = ()):
        self.rules: dict[str, PolicyRule] = {}
        for rule in rules:
            self.add(rule)

    def add(self, rule: PolicyRule) -> None:
        if rule.rule_id in self.rules:
            raise ValueError("duplicate policy rule")
        self.rules[rule.rule_id] = rule

    def remove(self, rule_id: str) -> None:
        self.rules.pop(rule_id, None)

    def _time_matches(self, rule: PolicyRule, ctx: PolicyContext) -> bool:
        if rule.valid_from is not None and ctx.at < rule.valid_from:
            return False
        if rule.valid_until is not None and ctx.at >= rule.valid_until:
            return False
        if rule.start_local is None and rule.end_local is None:
            return True
        local = ctx.at.timetz().replace(tzinfo=None)
        start = rule.start_local or time.min
        end = rule.end_local or time.max
        if start <= end:
            return start <= local < end
        return local >= start or local < end

    def _matches(self, rule: PolicyRule, ctx: PolicyContext) -> bool:
        if rule.operations and ctx.operation not in rule.operations:
            return False
        if rule.airport is not None and rule.airport != ctx.airport:
            return False
        if rule.terminal is not None and rule.terminal != ctx.terminal:
            return False
        if not self._time_matches(rule, ctx):
            return False
        if rule.required_attribute is not None:
            if rule.required_attribute not in ctx.attributes:
                return False
            if rule.required_value is not None and ctx.attributes[rule.required_attribute] != rule.required_value:
                return False
        return True

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        matched = [r for r in self.rules.values() if self._matches(r, ctx)]
        matched.sort(key=lambda r: (-r.priority, r.rule_id))
        denies = [r for r in matched if r.effect == Effect.DENY]
        requirements = [r for r in matched if r.effect == Effect.REQUIRE]
        allows = [r for r in matched if r.effect == Effect.ALLOW]
        unsatisfied = []
        for rule in requirements:
            marker = f"requirement:{rule.rule_id}"
            if not bool(ctx.attributes.get(marker, False)):
                unsatisfied.append(rule)
        if denies:
            allowed = False
        elif unsatisfied:
            allowed = False
        elif allows:
            allowed = True
        else:
            allowed = True
        reasons = tuple(r.reason for r in denies + unsatisfied)
        return PolicyDecision(
            allowed,
            tuple(r.rule_id for r in matched),
            tuple(r.rule_id for r in requirements),
            reasons,
        )

    def explain(self, ctx: PolicyContext) -> Mapping[str, object]:
        decision = self.evaluate(ctx)
        return {
            "operation": ctx.operation,
            "flight_id": ctx.flight_id,
            "allowed": decision.allowed,
            "matched_rules": decision.matched_rules,
            "requirements": decision.requirements,
            "reasons": decision.reasons,
        }
