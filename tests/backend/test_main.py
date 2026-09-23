"""Behavior-focused checks for the Python bridge; run only in joined proof."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from plugins.ctrlissues.backend import main


class WorkContractTests(unittest.TestCase):
    def setUp(self):
        self.payload = {
            "schema": "ctrlissues.work.v1", "id": "sample", "phase": "build",
            "context": {"request": "Read one file", "cwd": "."},
            "nodes": [{"id": "read", "kind": "observe",
                       "action": {"op": "stat", "path": "input.txt"},
                       "expect": {"path": ["result", "exists"], "equals": True}}],
        }

    def test_alias_and_closed_postcondition(self):
        self.payload["schema"] = "jmt.work.v1"
        nodes, _ = main.validate(self.payload, None)
        self.assertEqual(set(nodes), {"read"})
        self.assertFalse(main.postcondition(nodes["read"], {"exit_code": 0}))
        self.assertIsNone(main.postcondition({"id": "without-expectation"}, {"exit_code": 0}))
        self.assertFalse(main.postcondition(nodes["read"], {"result": {"exists": False}}))

    def test_expected_nonzero_check_exit_is_a_verified_postcondition(self):
        node = {"id": "expected-failure", "kind": "check",
                "action": {"op": "argv", "argv": ["false"], "check_kind": "smoke"},
                "expect": {"exit_code": 1}}
        main.validate_action(node, os.getcwd())
        self.assertTrue(main.postcondition(node, {"exit_code": 1}))

    def test_build_check_requires_smoke_or_safety_and_observe_phase_can_coexist(self):
        cursor = {"schema": "ctrlissues.cursor.v1", "phase": "build", "session_id": "s",
                  "revision": 2, "generation": 3}
        with self.assertRaisesRegex(ValueError, "construction_check_kind_required"):
            main.validate({**self.payload, "phase": "build", "nodes": [{"id": "c", "kind": "check",
                "action": {"op": "argv", "argv": ["true"]}}]}, cursor)
        observe = {**self.payload, "phase": "observe"}
        self.assertIn("read", main.validate(observe, cursor)[0])

    def test_context_contradiction_and_unknown_choice_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "phase_conflict"):
            main.validate(self.payload, {"schema": "ctrlissues.cursor.v1", "phase": "prove",
                                         "revision": 1, "generation": 1, "session_id": "s"})
        self.payload["nodes"][0]["action"] = {"op": "execute", "name": "guess"}
        nodes, _ = main.validate(self.payload, None)
        with self.assertRaisesRegex(ValueError, "unsupported_operation"):
            main.validate_action(nodes["read"], os.getcwd())

    def test_same_complete_input_replays_without_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / "input.txt").write_text("ok")
            self.payload["context"]["cwd"] = tmp
            raw = json.dumps(self.payload, sort_keys=True)
            first = main.run_work(self.payload, None, raw, tmp)
            second = main.run_work(self.payload, None, raw, tmp)
            self.assertEqual(first["status"], "complete")
            self.assertEqual(second["executed"], 0)
            self.assertTrue(second["replayed"])
            self.assertEqual(second["source_generation"], first["source_generation"])
            (root / "input.txt").write_text("changed content")
            third = main.run_work(self.payload, None, raw, tmp)
            self.assertFalse(third.get("replayed", False))
            self.assertEqual(third["executed"], 1)

    def test_deterministic_action_does_not_need_semantic_provider(self):
        payload = {"schema": "ctrlissues.work.v1", "id": "deterministic", "phase": "build",
                   "context": {"request": "Run a fixed local action", "cwd": ".", "session_id": "offline-task"},
                   "nodes": [{"id": "write", "kind": "action", "action": {"op": "argv", "argv": ["true"]}}]}
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "semantic_wave", side_effect=AssertionError("provider path used")), \
                patch.object(main, "execute", return_value={"exit_code": 0}) as execute:
            result = main.run_work(payload, None, json.dumps(payload), tmp)
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["executed"], 1)
            execute.assert_called_once()

    def test_action_idempotency_ignores_revision_but_rejects_changed_inputs_or_generation(self):
        payload = {"schema": "ctrlissues.work.v1", "id": "stable-action", "phase": "build",
                   "context": {"request": "Run a fixed local action", "cwd": "."},
                   "nodes": [{"id": "write", "kind": "action", "inputs": {"source": "v1"},
                              "action": {"op": "argv", "argv": ["true"]}}]}
        cursor = {"schema": "ctrlissues.cursor.v1", "phase": "build", "session_id": "task-1",
                  "revision": 4, "generation": 7}
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "execute", return_value={"exit_code": 0}) as execute:
            first = main.run_work(payload, cursor, json.dumps(payload), tmp)
            later_revision = dict(cursor, revision=5)
            replay = main.run_work(payload, later_revision, json.dumps(payload), tmp)
            self.assertTrue(replay["replayed"])
            self.assertEqual(replay["executed"], 0)
            self.assertEqual(execute.call_count, 1)
            changed = json.loads(json.dumps(payload))
            changed["nodes"][0]["inputs"]["source"] = "v2"
            rejected_input = main.run_work(changed, later_revision, json.dumps(changed), tmp)
            self.assertEqual(rejected_input["status"], "unknown")
            self.assertEqual(rejected_input["executed"], 0)
            newer_generation = dict(cursor, revision=6, generation=8)
            rejected_generation = main.run_work(payload, newer_generation, json.dumps(payload), tmp)
            self.assertEqual(rejected_generation["status"], "unknown")
            self.assertEqual(rejected_generation["executed"], 0)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(first["status"], "complete")

    def test_deterministic_branch_runs_when_semantic_branch_is_unassessed(self):
        payload = {"schema": "ctrlissues.work.v1", "id": "mixed-branches", "phase": "build",
                   "context": {"request": "Run a fixed action and assess supplied choices", "cwd": ".", "session_id": "mixed-task"},
                   "nodes": [{"id": "fixed", "kind": "action", "action": {"op": "argv", "argv": ["true"]}},
                             {"id": "choice-a", "kind": "action", "choice_group": "pick-one",
                              "action": {"op": "argv", "argv": ["true"]}},
                             {"id": "choice-b", "kind": "action", "choice_group": "pick-one",
                              "action": {"op": "argv", "argv": ["true"]}}]}
        unassessed = ([], {}, [], "typed_decision_unassessed", 0)
        with tempfile.TemporaryDirectory() as tmp, patch.object(main, "semantic_wave", return_value=unassessed), \
                patch.object(main, "execute", return_value={"exit_code": 0}) as execute:
            result = main.run_work(payload, None, json.dumps(payload), tmp)
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["receipts"]["fixed"]["status"], "executed")
            self.assertEqual(result["pending"], ["choice-a", "choice-b"])
            self.assertEqual(execute.call_count, 1)

    def test_independent_declared_resources_can_overlap(self):
        with tempfile.TemporaryDirectory() as tmp:
            barrier = threading.Barrier(2)
            def work(resource):
                with main.resource_leases([resource], Path(tmp), 2):
                    barrier.wait(timeout=1)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(work, "page:a"), pool.submit(work, "page:b")]
                [future.result() for future in futures]

    def test_existing_report_parent_mode_is_not_changed(self):
        if os.name == "nt": self.skipTest("POSIX mode assertion")
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "caller-owned"; parent.mkdir(mode=0o755)
            before = parent.stat().st_mode & 0o777
            report = main.write_report(parent / "report.json", {"status": "pending"})
            self.assertEqual(parent.stat().st_mode & 0o777, before)
            self.assertEqual(Path(report).stat().st_mode & 0o777, 0o600)

    def test_unknown_cli_flags_are_rejected(self):
        with self.assertRaises(SystemExit) as raised:
            main.main(["--data-dir", "/unused", "work", "--input", "-", "--report", "/unused/report", "--mystery"])
        self.assertEqual(raised.exception.code, 2)

    def test_decision_questions_use_closed_typed_choice_schema(self):
        semantics, _ = main.load_semantics()
        semantics.decisions._questions({"pick": {"type": "choice", "instructions": "Pick one.",
                                                     "criteria": {"a": "A", "abstain": "No choice"}}})
        with self.assertRaises(ValueError):
            semantics.decisions._questions({"pick": {"type": "freeform", "instructions": "Write anything."}})


if __name__ == "__main__":
    unittest.main()
