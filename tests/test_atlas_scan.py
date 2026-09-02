"""Tests for the architecture-atlas scanner.

The atlas is only worth looking at if its numbers are the repository's own.
These tests check the scanner against the code it reads rather than against a
frozen snapshot, so a real change to the harness shows up as a data change and
not as a test failure.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNER = ROOT / "docs" / "atlas" / "scripts" / "scan_harness.py"
DATA = ROOT / "docs" / "atlas" / "public" / "atlas.json"


def load_scanner():
    spec = importlib.util.spec_from_file_location("scan_harness", SCANNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scan_harness"] = module
    spec.loader.exec_module(module)
    return module


class AtlasScannerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scan = load_scanner()
        cls.atlas = cls.scan.build()

    def test_every_module_in_the_package_is_on_the_canvas(self) -> None:
        package = ROOT / "harness"
        on_disk = {
            ".".join(path.relative_to(package).with_suffix("").parts)
            for path in package.rglob("*.py")
            if path.stem not in {"__init__", "__main__"}
        }
        scanned = {m["name"] for m in self.atlas["modules"]}
        self.assertEqual(on_disk, scanned)

    def test_subpackage_modules_keep_their_real_path(self) -> None:
        by_name = {m["name"]: m for m in self.atlas["modules"]}
        self.assertIn("repl.agent", by_name)
        self.assertEqual(by_name["repl.agent"]["path"], "harness/repl/agent.py")
        self.assertTrue((ROOT / by_name["repl.agent"]["path"]).is_file())

    def test_a_relative_import_binds_to_the_sibling_not_a_top_level_name(self) -> None:
        """`from .tools import ...` inside harness/repl is repl.tools, not tools."""
        by_name = {m["name"]: m for m in self.atlas["modules"]}
        self.assertIn("repl.tools", by_name["repl.agent"]["imports"])
        self.assertNotIn("tools", by_name["repl.agent"]["imports"])

    def test_every_module_lands_in_exactly_one_layer(self) -> None:
        placed: list[str] = []
        for layer in self.atlas["layers"]:
            placed.extend(layer["modules"])
        self.assertEqual(len(placed), len(set(placed)), "a module is in two layers")
        self.assertEqual(set(placed), {m["name"] for m in self.atlas["modules"]})

    def test_import_edges_are_symmetric(self) -> None:
        """Whatever A says it imports, B has to agree it is imported by."""
        by_name = {m["name"]: m for m in self.atlas["modules"]}
        for module in self.atlas["modules"]:
            for target in module["imports"]:
                self.assertIn(
                    module["name"],
                    by_name[target]["imported_by"],
                    f"{module['name']} imports {target} but is not listed as an importer",
                )

    def test_no_module_imports_itself(self) -> None:
        for module in self.atlas["modules"]:
            self.assertNotIn(module["name"], module["imports"])

    def test_tools_match_the_registry(self) -> None:
        from harness.core.contracts import RiskLevel

        names = {t["name"] for t in self.atlas["tools"]}
        # These are the tools a model can always be offered; the scanner must
        # find each of them by reading the literal ToolSpec calls.
        self.assertLessEqual(
            {"repo_tree", "read_file", "apply_patch", "run_command", "finish"},
            names,
        )
        valid = {r.value for r in RiskLevel}
        for tool in self.atlas["tools"]:
            self.assertIn(tool["risk"], valid, tool["name"])
            self.assertLessEqual(set(tool["required"]), set(tool["arguments"]), tool["name"])

    def test_finish_is_the_control_tool(self) -> None:
        finish = next(t for t in self.atlas["tools"] if t["name"] == "finish")
        self.assertEqual(finish["risk"], "control")

    def test_enums_come_from_the_contracts(self) -> None:
        from harness.core.contracts import ActionKind, RiskLevel, RunStatus

        self.assertEqual(self.atlas["statuses"], [s.value for s in RunStatus])
        self.assertEqual(self.atlas["actions"], [a.value for a in ActionKind])
        self.assertEqual(self.atlas["risks"], [r.value for r in RiskLevel])

    def test_terminal_events_are_found(self) -> None:
        terminal = {e["type"] for e in self.atlas["events"] if e["terminal"]}
        self.assertEqual(
            terminal,
            {
                "RUN_COMPLETED",
                "RUN_FAILED",
                "RUN_BLOCKED",
                "RUN_BUDGET_EXHAUSTED",
                "RUN_CANCELLED",
            },
        )

    def test_every_event_has_a_writer(self) -> None:
        self.assertTrue(self.atlas["events"])
        for event in self.atlas["events"]:
            self.assertTrue(event["writers"], event["type"])

    def test_cli_verbs_match_the_parser(self) -> None:
        names = {c["name"] for c in self.atlas["commands"]}
        self.assertLessEqual({"run", "resume", "doctor", "inspect", "replay"}, names)

    def test_boundary_stages_all_name_a_real_module(self) -> None:
        known = {m["name"] for m in self.atlas["modules"]}
        for stage in self.atlas["boundary"]:
            self.assertTrue(stage["present"], stage["stage"])
            self.assertIn(stage["module"], known)

    def test_totals_agree_with_the_modules(self) -> None:
        totals = self.atlas["totals"]
        modules = self.atlas["modules"]
        self.assertEqual(totals["modules"], len(modules))
        self.assertEqual(totals["sloc"], sum(m["sloc"] for m in modules))
        self.assertEqual(totals["edges"], sum(len(m["imports"]) for m in modules))
        for module in modules:
            self.assertLessEqual(module["sloc"], module["lines"])

    def test_the_scan_is_deterministic(self) -> None:
        first = json.dumps(self.scan.build(), sort_keys=True)
        second = json.dumps(self.scan.build(), sort_keys=True)
        self.assertEqual(first, second)

    def test_check_mode_reports_a_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "atlas.json"
            path.write_text("{}", encoding="utf-8")
            argv = sys.argv
            sys.argv = ["scan_harness.py", "--out", str(path), "--check"]
            try:
                self.assertEqual(self.scan.main(), 1)
                sys.argv = ["scan_harness.py", "--out", str(path)]
                self.assertEqual(self.scan.main(), 0)
                sys.argv = ["scan_harness.py", "--out", str(path), "--check"]
                self.assertEqual(self.scan.main(), 0)
            finally:
                sys.argv = argv

    def test_the_committed_data_file_is_readable(self) -> None:
        self.assertTrue(DATA.exists(), "run scan_harness.py to write public/atlas.json")
        data = json.loads(DATA.read_text(encoding="utf-8"))
        self.assertIn("modules", data)
        self.assertIn("totals", data)



class TaxonomyTests(unittest.TestCase):
    """The editorial half. Every name it uses must resolve to real code."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.scan = load_scanner()
        cls.atlas = cls.scan.build()
        cls.known = {m["name"] for m in cls.atlas["modules"]}

    def test_every_primitive_names_modules_that_exist(self) -> None:
        """A rename would otherwise turn a covered primitive into a fake gap."""
        for row in self.atlas["primitives"]:
            for loop in ("batch", "repl"):
                self.assertEqual(row[loop]["missing"], [], f"{row['key']}.{loop}")

    def test_a_primitive_is_covered_by_at_least_one_loop(self) -> None:
        for row in self.atlas["primitives"]:
            self.assertTrue(
                row["batch"]["modules"] or row["repl"]["modules"], row["key"]
            )

    def test_cell_totals_match_the_modules_they_name(self) -> None:
        by_name = {m["name"]: m for m in self.atlas["modules"]}
        for row in self.atlas["primitives"]:
            for loop in ("batch", "repl"):
                cell = row[loop]
                self.assertEqual(
                    cell["sloc"], sum(by_name[n]["sloc"] for n in cell["modules"])
                )

    def test_primitive_keys_are_unique(self) -> None:
        keys = [r["key"] for r in self.atlas["primitives"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_every_turn_step_names_a_real_module_and_lane(self) -> None:
        lanes = {lane["key"] for lane in self.atlas["lanes"]}
        for step in self.atlas["steps"]:
            self.assertTrue(step["present"], step["module"])
            self.assertIn(step["at"], lanes, step["n"])
            self.assertIn(step["to"], lanes, step["n"])

    def test_turn_steps_are_numbered_in_order(self) -> None:
        numbers = [s["n"] for s in self.atlas["steps"]]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers, list(range(1, len(numbers) + 1)))

    def test_every_emitted_event_is_one_the_code_writes(self) -> None:
        """The sequence diagram must not invent event names."""
        real = {e["type"] for e in self.atlas["events"]}
        for step in self.atlas["steps"]:
            if step["emits"]:
                self.assertIn(step["emits"], real, f"step {step['n']}")

    def test_every_gate_names_a_real_module(self) -> None:
        for gate in self.atlas["gates"]:
            self.assertTrue(gate["present"], gate["gate"])
            self.assertIn(gate["module"], self.known)

    def test_transitions_only_use_statuses_from_the_contract(self) -> None:
        statuses = set(self.atlas["statuses"])
        for move in self.atlas["transitions"]:
            self.assertIn(move["from"], statuses)
            self.assertIn(move["to"], statuses)

    def test_the_state_columns_cover_every_status_exactly_once(self) -> None:
        laid_out = [s for column in self.atlas["state_columns"] for s in column]
        self.assertEqual(sorted(laid_out), sorted(self.atlas["statuses"]))

    def test_every_terminal_status_is_in_the_last_column(self) -> None:
        terminal = {
            e["type"].removeprefix("RUN_")
            for e in self.atlas["events"]
            if e["terminal"]
        }
        self.assertTrue(terminal.issubset(set(self.atlas["state_columns"][-1])))

    def test_both_loops_exist_in_the_code(self) -> None:
        self.assertEqual(len(self.atlas["loops"]), 2)
        for loop in self.atlas["loops"]:
            self.assertTrue(loop["present"], loop["key"])
            self.assertIn(loop["root"], self.known)
            self.assertIn(loop["entry"], self.known)

    def test_the_shared_modules_are_imported_by_both_loops(self) -> None:
        """The seam is only a seam if both sides actually reach it."""
        self.assertTrue(self.atlas["shared"])
        for name in self.atlas["shared"]:
            self.assertIn(name, self.known)

    def test_the_two_loops_do_not_import_each_other(self) -> None:
        """The whole claim of the loops diagram."""
        by_name = {m["name"]: m for m in self.atlas["modules"]}
        repl = {n for n in self.known if n.startswith("repl.")}
        for name in repl:
            for target in by_name[name]["imports"]:
                self.assertNotEqual(target, "runtime", f"{name} imports runtime")
        for name in self.known - repl:
            for target in by_name[name]["imports"]:
                self.assertFalse(
                    target.startswith("repl."), f"{name} imports {target}"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
