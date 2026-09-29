import unittest
from datetime import datetime, timezone

from rampflow.models import Decision
from rampflow.workflows.control import RampWorkflowControl
from rampflow.workflows.control import WorkflowRejected


class WorkflowControlTests(unittest.TestCase):
    def setUp(self):
        self.at = datetime(2026, 1, 1, 10, tzinfo=timezone.utc)

    def test_success_is_idempotent_and_publishes_once(self):
        control = RampWorkflowControl()
        calls = []

        first = control._guard(
            "op-1", "workflow.smoke", "M1", self.at,
            lambda: calls.append("commit") or "ok",
        )
        second = control._guard(
            "op-1", "workflow.smoke", "M1", self.at,
            lambda: calls.append("duplicate"),
        )

        self.assertIs(first, second)
        self.assertEqual(calls, ["commit"])
        self.assertEqual(control.stream.head, 1)
        self.assertEqual(first.result, "ok")

    def test_failed_domain_change_has_no_receipt_or_event(self):
        control = RampWorkflowControl()

        def rejected():
            raise RuntimeError("domain rejected")

        with self.assertRaises(RuntimeError):
            control._guard("op-2", "workflow.smoke", "F1", self.at, rejected)
        self.assertEqual(control.stream.head, 0)
        self.assertEqual(control.receipts, {})

    def test_operation_id_cannot_change_identity(self):
        control = RampWorkflowControl()
        control._guard("op-3", "workflow.smoke", "C1", self.at, lambda: True)
        with self.assertRaises(ValueError):
            control._guard("op-3", "workflow.smoke", "C2", self.at, lambda: True)

    def test_rejected_dependency_prevents_domain_commit(self):
        control = RampWorkflowControl()
        calls = []
        denied = Decision(False, "stand-conflict", "dependent plan is stale")
        with self.assertRaises(WorkflowRejected):
            control._guard(
                "op-4", "workflow.smoke", "F4", self.at,
                lambda: calls.append("committed"), requirements=(denied,),
            )
        self.assertEqual(calls, [])
        self.assertEqual(control.stream.head, 0)


if __name__ == "__main__":
    unittest.main()
