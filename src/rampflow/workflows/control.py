from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Mapping, TypeVar

from rampflow.event_stream import EventStream, StreamEvent
from rampflow.models import Decision
from rampflow.timeutil import utc


T = TypeVar("T")


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    topic: str
    subject: str
    event: StreamEvent
    result: object


class WorkflowRejected(RuntimeError):
    def __init__(self, decisions: tuple[Decision, ...]):
        self.decisions = decisions
        summary = "; ".join(f"{item.code}: {item.reason}" for item in decisions)
        super().__init__(summary)


class RampWorkflowControl:
    """Commits domain changes and their outward operational event as one workflow.

    Domain services retain their own concurrency and validation rules.  This layer
    supplies request idempotency and ensures a rejected domain operation cannot be
    advertised to downstream consumers as a successful change.
    """

    def __init__(self, stream: EventStream | None = None):
        self.stream = stream or EventStream()
        self._receipts: dict[str, OperationReceipt] = {}

    @property
    def receipts(self) -> Mapping[str, OperationReceipt]:
        return dict(self._receipts)

    def _guard(
        self,
        operation_id: str,
        topic: str,
        subject: str,
        at: datetime,
        action: Callable[[], T],
        evidence: Mapping[str, object] | None = None,
        requirements: tuple[Decision, ...] = (),
    ) -> OperationReceipt:
        previous = self._receipts.get(operation_id)
        if previous is not None:
            if previous.topic != topic or previous.subject != subject:
                raise ValueError("operation id reused for a different workflow")
            return previous
        rejected = tuple(item for item in requirements if not item.accepted)
        if rejected:
            raise WorkflowRejected(rejected)
        result = action()
        payload = {"operation_id": operation_id, **dict(evidence or {})}
        event = self.stream.publish(operation_id, utc(at), topic, subject, payload)
        receipt = OperationReceipt(operation_id, topic, subject, event, result)
        self._receipts[operation_id] = receipt
        return receipt

    def commit_departure_movement(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "departure.movement.committed", subject, at, action, requirements=requirements)

    def commit_stand_service_package(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "stand.service.package.committed", subject, at, action, requirements=requirements)

    def assign_mobile_crew(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "mobile.crew.assignment.committed", subject, at, action, requirements=requirements)

    def reconcile_fuel_load(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "fuel.load.reconciliation.committed", subject, at, action, requirements=requirements)

    def commit_turnaround_dispatch(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "turnaround.dispatch.committed", subject, at, action, requirements=requirements)

    def publish_recovery_stand_plan(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "recovery.stand.plan.published", subject, at, action, requirements=requirements)

    def update_connection_transport(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "connection.transport.updated", subject, at, action, requirements=requirements)

    def acknowledge_stream_projection(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "stream.projection.acknowledged", subject, at, action, requirements=requirements)

    def apply_restriction_policy(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "restriction.policy.applied", subject, at, action, requirements=requirements)

    def commit_turnaround_service_windows(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        event = self.stream.publish(operation_id, utc(at), "turnaround.service.windows.committed", subject, {"operation_id": operation_id})
        result = action()
        receipt = OperationReceipt(operation_id, event.topic, subject, event, result)
        self._receipts[operation_id] = receipt
        return receipt

    def revise_deicing_departure(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "deicing.departure.revised", subject, at, action, requirements=requirements)

    def replay_baggage_connection(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "baggage.connection.replayed", subject, at, action, requirements=requirements)

    def commit_hazmat_load(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "hazmat.policy.load.committed", subject, at, action, requirements=requirements)

    def commit_fuel_ledger_batch(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "fuel.ledger.batch.committed", subject, at, action, requirements=requirements)

    def commit_turnaround_stand(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "turnaround.stand.committed", subject, at, action, requirements=requirements)

    def commit_deicing_runway_slot(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "deicing.runway.slot.committed", subject, at, action, requirements=requirements)

    def apply_notam_stand_restriction(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "notam.stand.restriction.applied", subject, at, action, requirements=requirements)

    def release_maintenance_turnaround(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "maintenance.turnaround.release.decided", subject, at, action, requirements=requirements)

    def commit_deicing_dispatch(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "deicing.dispatch.committed", subject, at, action, requirements=requirements)

    def publish_apron_movement_safety(self, operation_id: str, subject: str, at: datetime, action: Callable[[], T], requirements: tuple[Decision, ...] = ()) -> OperationReceipt:
        return self._guard(operation_id, "apron.movement.safety.decided", subject, at, action, requirements=requirements)
