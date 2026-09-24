#!/usr/bin/env python3
"""Offline tests for jev.py's routing policy (decide()). No Jev calls: signals are supplied directly."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import jev  # noqa: E402


def sig(**kw):
    base = dict(self_contained=0.9, read_only=0.05, high_stakes=0.1, unknown_cause=0.05, design=0.05,
                batchable=0.2, exhaustive=0.1)
    base.update(kw)
    return base


def route(task="do the thing", depth=1.0, conf=0.9, breadth=1.0, **signals):
    d, reasons, writes = jev.decide(task, depth, conf, breadth, sig(**signals))
    return d["tier"], d, reasons


class Ladders(unittest.TestCase):
    def test_read_only_lookup_and_deep_read(self):
        self.assertEqual(route(depth=0.2, read_only=0.95)[0], "scout")
        self.assertEqual(route(depth=2.5, read_only=0.95, high_stakes=0.99, unknown_cause=0.9)[0], "analyst")

    def test_explicit_no_edit_forces_read_ladder(self):
        for task in ["Review the interrupt handlers and write up races. Don't fix anything yet.",
                     "Investigate the leak, no changes please", "read-only: explain the auth flow",
                     "Just explain how refunds work", "do not modify files, report only"]:
            tier, d, _ = route(task, depth=3.0, read_only=0.3, unknown_cause=0.9, high_stakes=0.9)
            self.assertEqual(d["ladder"], "read", task)

    def test_lean_read_only(self):
        self.assertEqual(route(depth=2.0, read_only=0.65)[1]["ladder"], "read")

    def test_escalation_never_crosses_ladders(self):
        for tier, nxt in jev.ESCALATE.items():
            self.assertEqual(jev.TIERS[tier]["ladder"] == "read", jev.TIERS[nxt]["ladder"] == "read", tier)
        tier, d, _ = route(depth=0.2, conf=0.3, read_only=0.95)
        self.assertEqual(d["ladder"], "read")


class WriteTiers(unittest.TestCase):
    def test_unknown_cause_is_debugger_even_when_high_stakes(self):
        self.assertEqual(route(depth=3.0, unknown_cause=0.9, high_stakes=0.97)[0], "debugger")

    def test_strong_stakes_known_approach_is_architect(self):
        self.assertEqual(route(depth=2.2, high_stakes=0.93)[0], "architect")

    def test_moderate_stakes_is_engineer_not_architect(self):
        self.assertEqual(route(depth=2.0, high_stakes=0.72)[0], "engineer")

    def test_advice_only_design_question_is_read_only_advisor(self):
        tier, d, _ = route("Oban or Broadway for background jobs? Recommend one.", depth=3.0, design=0.98, read_only=0.94)
        self.assertEqual((tier, d["ladder"], d["effort"]), ("advisor", "read", "max"))

    def test_design_is_architect(self):
        self.assertEqual(route(depth=2.9, design=0.98, read_only=0.58)[0], "architect")

    def test_small_and_routine(self):
        self.assertEqual(route(depth=0.1)[0], "builder")
        self.assertEqual(route(depth=1.8)[0], "engineer")


class Orchestration(unittest.TestCase):
    def test_exhaustive_non_batchable_is_ultracode(self):
        self.assertEqual(route(depth=2.9, breadth=3.0, exhaustive=0.9, batchable=0.1, high_stakes=0.9)[0], "ultracode")

    def test_wide_repeated_change_stays_single_agent(self):
        self.assertNotEqual(route(depth=2.0, breadth=2.9, exhaustive=0.9, batchable=0.8)[0], "ultracode")

    def test_wide_single_flow_stays_single_agent(self):
        self.assertEqual(route(depth=2.8, breadth=2.9, read_only=0.9, exhaustive=0.2)[0], "analyst")

    def test_not_self_contained_stays_in_main(self):
        self.assertEqual(route(depth=1.5, self_contained=0.1)[1]["via"], "main")


if __name__ == "__main__":
    unittest.main(verbosity=1)
