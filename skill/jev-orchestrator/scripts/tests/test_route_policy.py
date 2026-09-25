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
    def test_unknown_cause_plans_first_then_low_code(self):
        tier, d, _ = route(depth=3.0, unknown_cause=0.9, high_stakes=0.97)
        self.assertEqual((tier, d["effort"]), ("builder", "low"))
        self.assertEqual(d["plan_first"]["planner"], "jev-analyst")

    def test_strong_stakes_known_approach_is_architect(self):
        self.assertEqual(route(depth=2.2, high_stakes=0.93)[0], "architect")

    def test_moderate_stakes_does_not_raise_code_effort(self):
        self.assertIn(route(depth=2.0, high_stakes=0.72)[1]["effort"], ("low", "medium"))

    def test_advice_only_design_question_is_read_only_advisor(self):
        tier, d, _ = route("Oban or Broadway for background jobs? Recommend one.", depth=3.0, design=0.98, read_only=0.94)
        self.assertEqual((tier, d["ladder"], d["effort"]), ("advisor", "read", "max"))

    def test_design_is_architect(self):
        self.assertEqual(route(depth=2.9, design=0.98, read_only=0.58)[0], "architect")

    def test_small_and_routine(self):
        self.assertEqual(route(depth=0.1)[0], "builder")
        self.assertEqual(route(depth=1.8)[0], "builder")
        self.assertEqual(route(depth=2.2)[1]["effort"], "medium")


class EffortRule(unittest.TestCase):
    CODE_EFFORTS = ("low", "medium", "high")

    def test_code_tiers_never_above_high(self):
        for tier, t in jev.TIERS.items():
            if t["ladder"] == "write":
                self.assertIn(t["effort"], self.CODE_EFFORTS, tier)

    def test_review_task_routes_to_reviewer_medium(self):
        tier, d, _ = route("Review this diff for correctness bugs, report only", depth=2.2, read_only=0.95)
        self.assertEqual((tier, d["effort"]), ("reviewer", "medium"))

    def test_reviewer_is_medium(self):
        self.assertEqual(jev.TIERS["reviewer"]["effort"], "medium")

    def test_first_route_never_picks_debugger(self):
        for kw in [dict(depth=3.0, unknown_cause=0.9), dict(depth=2.9), dict(depth=2.4, high_stakes=0.95), dict(depth=0.3, conf=0.2)]:
            tier, d, _ = route(**kw)
            self.assertNotEqual(tier, "debugger", kw)
            self.assertNotIn(d["effort"], ("xhigh", "max")) if d["ladder"] == "write" else None

    def test_architect_plans_then_builder_low(self):
        tier, d, _ = route(depth=2.9, design=0.98, read_only=0.3)
        self.assertEqual((tier, d["ladder"]), ("architect", "plan"))
        self.assertEqual((d["plan_first"]["implementer"], d["plan_first"]["implementer_effort"]), ("jev-builder", "low"))

    def test_code_escalates_low_medium_high(self):
        self.assertEqual([jev.TIERS[t]["effort"] for t in ("builder", "engineer", "debugger")], ["low", "medium", "high"])
        self.assertEqual((jev.ESCALATE["builder"], jev.ESCALATE["engineer"]), ("engineer", "debugger"))


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
