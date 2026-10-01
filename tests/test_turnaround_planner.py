import unittest
from dataclasses import replace
from datetime import datetime, timezone

from rampflow.timeutil import Window
from rampflow.models import TaskSpec, ServiceState
from rampflow.turnaround import (
    TurnaroundPlanner, TurnTask, ResourceRequirement,
)

UTC = timezone.utc
D = lambda h, m=0: datetime(2026, 1, 1, h, m, tzinfo=UTC)


def exclusive_task(task_id="load", duration=30, predecessors=frozenset(),
                   can_overlap_with=frozenset(), kind="belt"):
    return TurnTask(
        TaskSpec(task_id, duration, predecessors=predecessors),
        requirements=(ResourceRequirement(kind),),
        can_overlap_with=can_overlap_with,
    )


class TurnaroundReservationTests(unittest.TestCase):
    def test_stale_plan_cannot_double_book_device(self):
        # Two mutually exclusive safeguard items each found a window on their
        # own; the second one submits against the old resource generation.
        planner = TurnaroundPlanner({"belt": ["BELT-1"]})
        first = planner.plan("F1", [exclusive_task()], D(10), D(12))
        self.assertTrue(planner.commit(first).accepted)

        stale = replace(planner.plan("F2", [exclusive_task()], D(10), D(12)),
                        resource_generation=0)
        decision = planner.commit(stale)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "stale-plan")
        # no half-feasible residue: plan and reservations both untouched
        self.assertNotIn("F2", planner.plans)
        self.assertEqual(len(planner.ledger.reservations("BELT-1")), 1)

        fresh = planner.plan("F2", [exclusive_task()], D(10), D(12))
        self.assertEqual(fresh.runs["load"].planned.start, D(10, 30))
        self.assertTrue(planner.commit(fresh).accepted)
        self.assertEqual(len(planner.ledger.reservations("BELT-1")), 2)

    def test_conflicting_current_generation_plan_rejected_entirely(self):
        planner = TurnaroundPlanner({"gpu": ["GPU-1"]})
        self.assertTrue(planner.commit(
            planner.plan("A", [exclusive_task(kind="gpu")], D(10), D(12))).accepted)
        plan = planner.plan("B", [exclusive_task(kind="gpu")], D(10), D(12))
        tampered = replace(plan, runs={
            "load": replace(plan.runs["load"], planned=Window(D(10), D(10, 30))),
        })
        decision = planner.commit(tampered)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.code, "resource-conflict")
        self.assertNotIn("B", planner.plans)
        self.assertEqual(len(planner.ledger.reservations("GPU-1")), 1)

    def test_multi_task_plan_is_all_or_nothing(self):
        planner = TurnaroundPlanner({"belt": ["BELT-1"], "gpu": ["GPU-1"]})
        planner.ledger.reserve("GPU-1", Window(D(10, 30), D(12)), "OTHER:z")
        tasks = [
            exclusive_task("a", kind="belt"),
            exclusive_task("b", predecessors=frozenset({"a"}), kind="gpu"),
        ]
        decision = planner.plan_and_commit("M", tasks, D(10), D(12))
        self.assertFalse(decision.accepted)
        self.assertNotIn("M", planner.plans)
        # the earlier task must not have leaked a reservation
        self.assertEqual(planner.ledger.reservations("BELT-1"), ())

    def test_overlap_exemption_must_be_mutual(self):
        bidirectional = [
            exclusive_task("a", can_overlap_with=frozenset({"b"})),
            exclusive_task("b", can_overlap_with=frozenset({"a"})),
        ]
        planner = TurnaroundPlanner({"belt": ["BELT-1"]})
        self.assertTrue(planner.plan_and_commit("T", bidirectional, D(10), D(10, 45)).accepted)

        one_sided = [
            exclusive_task("a", can_overlap_with=frozenset({"b"})),
            exclusive_task("b"),
        ]
        planner = TurnaroundPlanner({"belt": ["BELT-1"]})
        decision = planner.plan_and_commit("T", one_sided, D(10), D(10, 45))
        self.assertFalse(decision.accepted)
        self.assertNotIn("T", planner.plans)

    def test_non_exclusive_use_shares_device(self):
        shared = [TurnTask(
            TaskSpec("s", 30),
            requirements=(ResourceRequirement("power", exclusive=False),),
        )]
        planner = TurnaroundPlanner({"power": ["P1"]})
        self.assertTrue(planner.plan_and_commit("S1", shared, D(10), D(12)).accepted)
        self.assertTrue(planner.plan_and_commit("S2", shared, D(10), D(12)).accepted)
        self.assertFalse(planner.ledger.reservations("P1")[0].exclusive)
        # an exclusive hold still cannot overlap the shared uses
        with self.assertRaises(RuntimeError):
            planner.ledger.reserve("P1", Window(D(10, 15), D(10, 45)), "exclusive")

    def test_dependency_order_enforced_at_activation(self):
        planner = TurnaroundPlanner({"belt": ["BELT-1"], "gpu": ["GPU-1"]})
        tasks = [
            exclusive_task("a", kind="belt"),
            exclusive_task("b", predecessors=frozenset({"a"}), kind="gpu"),
        ]
        self.assertTrue(planner.plan_and_commit("DEP", tasks, D(10), D(12)).accepted)
        with self.assertRaises(RuntimeError):
            planner.transition("DEP", "b", ServiceState.READY)
        planner.transition("DEP", "a", ServiceState.READY)
        planner.transition("DEP", "a", ServiceState.ACTIVE)
        planner.transition("DEP", "a", ServiceState.COMPLETE)
        planner.transition("DEP", "b", ServiceState.READY)


if __name__ == "__main__":
    unittest.main()
