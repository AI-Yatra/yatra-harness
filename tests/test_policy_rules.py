"""Operator rules: allow, ask and deny for one specific thing.

A mode is a default for everything. A rule is a decision about one thing, so
it outranks the mode in both directions: a deny rule holds even in full-auto,
and an ask rule can make full-auto stop and ask about the one call the operator
cares about. Nothing reaches past the deny-list, which stays absolute.

`docker run` is used as the example of something the shipped deny-list does not
cover, so these tests show rules adding reach rather than restating it.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from harness.config import load_config
from harness.core.errors import ConfigurationError
from harness.execution.policy import denied_pattern, parse_rule, rule_for
from harness.execution.workspace import Workspace
from harness.repl.approvals import Gate, Mode, Verdict
from harness.repl.tools import ReplToolset

ROOT = Path(__file__).resolve().parents[1]


class ParseTests(unittest.TestCase):
    def test_a_tool_with_a_pattern(self) -> None:
        rule = parse_rule("run_command(git push *)", "deny")
        self.assertEqual((rule.effect, rule.tool, rule.pattern), ("deny", "run_command", "git push *"))

    def test_a_bare_tool_name_covers_every_call(self) -> None:
        rule = parse_rule("web_search", "ask")
        self.assertEqual(rule.tool, "web_search")
        self.assertEqual(rule.pattern, "")

    def test_a_star_matches_any_tool(self) -> None:
        self.assertEqual(parse_rule("*", "ask").tool, "*")

    def test_rubbish_is_refused_with_an_example(self) -> None:
        with self.assertRaises(ValueError) as caught:
            parse_rule("run_command(", "deny")
        self.assertIn("run_command(git push *)", str(caught.exception))

    def test_an_unknown_effect_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            parse_rule("run_command(ls)", "maybe")

    def test_the_text_round_trips_for_display(self) -> None:
        self.assertEqual(parse_rule("write_file(*.env)", "deny").text, "write_file(*.env)")
        self.assertEqual(parse_rule("web_search", "deny").text, "web_search")


class MatchTests(unittest.TestCase):
    def rules(self):
        return [
            parse_rule("run_command(docker *)", "deny"),
            parse_rule("write_file(*.env)", "deny"),
            parse_rule("run_command(git *)", "allow"),
            parse_rule("read_file(*secret*)", "ask"),
        ]

    def effect(self, tool: str, arguments: dict) -> str:
        found = rule_for(tool, arguments, self.rules())
        return found.effect if found else ""

    def test_a_command_pattern_matches_by_token(self) -> None:
        self.assertEqual(self.effect("run_command", {"command": ["docker", "run", "x"]}), "deny")
        self.assertEqual(self.effect("run_command", {"command": ["git", "status"]}), "allow")

    def test_a_command_that_matches_nothing_leaves_it_to_the_mode(self) -> None:
        self.assertEqual(self.effect("run_command", {"command": ["ls", "-la"]}), "")

    def test_a_wrapper_cannot_hide_from_a_rule(self) -> None:
        """The same expansion the deny-list uses, for the same reason."""
        self.assertEqual(
            self.effect("run_command", {"command": ["bash", "-c", "docker run x"]}), "deny"
        )
        self.assertEqual(self.effect("run_command", {"command": ["sudo", "docker", "ps"]}), "deny")

    def test_a_path_pattern_is_a_glob(self) -> None:
        self.assertEqual(self.effect("write_file", {"path": ".env"}), "deny")
        self.assertEqual(self.effect("write_file", {"path": "config/prod.env"}), "deny")
        self.assertEqual(self.effect("write_file", {"path": "src/a.py"}), "")

    def test_a_partial_word_does_not_match(self) -> None:
        """Tokens rather than characters: `git pushed` is not `git push`."""
        rules = [parse_rule("run_command(git push)", "deny")]
        self.assertIsNone(rule_for("run_command", {"command": ["git", "pushed"]}, rules))

    def test_deny_beats_ask_beats_allow_regardless_of_order(self) -> None:
        rules = [
            parse_rule("run_command(*)", "allow"),
            parse_rule("run_command(*)", "ask"),
            parse_rule("run_command(*)", "deny"),
        ]
        self.assertEqual(rule_for("run_command", {"command": ["ls"]}, rules).effect, "deny")
        self.assertEqual(rule_for("run_command", {"command": ["ls"]}, rules[:2]).effect, "ask")

    def test_a_string_command_is_matched_too(self) -> None:
        self.assertEqual(self.effect("run_command", {"command": "docker ps"}), "deny")

    def test_no_rules_means_no_opinion(self) -> None:
        self.assertIsNone(rule_for("run_command", {"command": ["ls"]}, []))


class GateTestCase(unittest.TestCase):
    RULES = (
        "run_command(docker *)",
        "write_file(*.env)",
    )

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        base = load_config(ROOT / "configs" / "ay.yaml")
        rules = (
            parse_rule("run_command(docker *)", "deny"),
            parse_rule("write_file(*.env)", "deny"),
            parse_rule("run_command(git *)", "allow"),
            parse_rule("read_file(*secret*)", "ask"),
        )
        self.config = replace(base, policy=replace(base.policy, rules=rules))
        toolset = ReplToolset(Workspace(self.root, ()), self.config)
        self.specs = {spec.name: spec for spec in toolset.specs()}
        self.asked: list[str] = []

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def gate(self, mode: Mode) -> Gate:
        def prompt(request):
            self.asked.append(request.question)
            return Verdict.ALLOW

        return Gate(self.config.policy, mode=mode, prompt=prompt)


class GateTests(GateTestCase):
    def test_docker_is_not_on_the_shipped_deny_list(self) -> None:
        """Otherwise these tests would prove nothing about rules."""
        self.assertIsNone(
            denied_pattern(("docker", "run", "x"), self.config.policy.denied_commands)
        )

    def test_a_deny_rule_holds_even_in_full_auto(self) -> None:
        decision = self.gate(Mode.FULL_AUTO).check(
            self.specs["run_command"], {"command": ["docker", "run", "x"]}
        )
        self.assertFalse(decision.allowed)
        self.assertIn("docker *", decision.reason)

    def test_a_deny_rule_is_never_offered_for_approval(self) -> None:
        self.gate(Mode.SUGGEST).check(
            self.specs["run_command"], {"command": ["docker", "run", "x"]}
        )
        self.assertEqual(self.asked, [])

    def test_an_allow_rule_skips_the_prompt_a_mode_would_have_shown(self) -> None:
        decision = self.gate(Mode.SUGGEST).check(
            self.specs["run_command"], {"command": ["git", "status"]}
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(self.asked, [])

    def test_an_ask_rule_stops_full_auto(self) -> None:
        """A rule can make things stricter without refusing them."""
        decision = self.gate(Mode.FULL_AUTO).check(
            self.specs["read_file"], {"path": "secrets.txt"}
        )
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.asked)
        self.assertEqual(len(self.asked), 1)

    def test_an_unmatched_call_still_follows_the_mode(self) -> None:
        allowed = self.gate(Mode.FULL_AUTO).check(
            self.specs["run_command"], {"command": ["ls"]}
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual(self.asked, [])

    def test_the_deny_list_still_outranks_an_allow_rule(self) -> None:
        """No rule may reach past it."""
        base = load_config(ROOT / "configs" / "ay.yaml")
        config = replace(
            base, policy=replace(base.policy, rules=(parse_rule("run_command(*)", "allow"),))
        )
        gate = Gate(config.policy, mode=Mode.FULL_AUTO)
        decision = gate.check(self.specs["run_command"], {"command": ["rm", "-rf", "/"]})
        self.assertFalse(decision.allowed)
        self.assertIn("deny-list", decision.reason)

    def test_reads_are_still_free_unless_a_rule_says_otherwise(self) -> None:
        decision = self.gate(Mode.SUGGEST).check(self.specs["read_file"], {"path": "a.py"})
        self.assertTrue(decision.allowed)
        self.assertFalse(decision.asked)

    def test_plan_mode_still_reads_when_a_rule_asks(self) -> None:
        """A read with an ask rule must not be refused for being plan mode."""
        decision = self.gate(Mode.PLAN).check(self.specs["read_file"], {"path": "secrets.txt"})
        self.assertTrue(decision.allowed)
        self.assertTrue(decision.asked)

    def test_plan_mode_still_refuses_writes_a_rule_would_allow(self) -> None:
        base = load_config(ROOT / "configs" / "ay.yaml")
        config = replace(
            base, policy=replace(base.policy, rules=(parse_rule("write_file(*)", "allow"),))
        )
        gate = Gate(config.policy, mode=Mode.PLAN)
        decision = gate.check(self.specs["write_file"], {"path": "a.py", "content": "x"})
        # An allow rule answers "may this be done at all". Plan mode is the
        # operator saying they do not want anything changed right now, which is
        # the wider question, so the mode wins.
        self.assertFalse(decision.allowed)
        self.assertIn("plan mode", decision.reason)


class ConfigTests(unittest.TestCase):
    #: The empty rules block the shipped config carries. Replaced wholesale so
    #: these tests edit the real key instead of adding a second one: a
    #: duplicate key in YAML is not an error, the later value simply wins,
    #: which would make every assertion here pass for the wrong reason.
    EMPTY = "  rules:\n    deny: []\n    ask: []\n    allow: []\n"

    def load(self, block: str):
        text = (ROOT / "configs" / "ay.yaml").read_text(encoding="utf-8")
        self.assertIn(self.EMPTY, text, "the shipped rules block moved")
        path = Path(tempfile.mkdtemp()) / "c.yaml"
        path.write_text(text.replace(self.EMPTY, block, 1), encoding="utf-8")
        return load_config(path)

    def test_rules_parse_from_yaml(self) -> None:
        config = self.load("  rules:\n    deny:\n      - run_command(docker *)\n    allow:\n      - run_command(git *)\n")
        self.assertEqual(len(config.policy.rules), 2)
        self.assertEqual({r.effect for r in config.policy.rules}, {"deny", "allow"})

    def test_omitting_them_is_fine(self) -> None:
        self.assertEqual(load_config(ROOT / "configs" / "ay.yaml").policy.rules, ())

    def test_a_bad_rule_fails_at_load_time(self) -> None:
        """Not at first use, when the operator believes it is protecting them."""
        with self.assertRaises(ConfigurationError) as caught:
            self.load("  rules:\n    deny:\n      - 'run_command('\n")
        self.assertIn("policy.rules.deny[0]", str(caught.exception))

    def test_an_unknown_effect_fails_at_load_time(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            self.load("  rules:\n    maybe:\n      - run_command(ls)\n")
        self.assertIn("unknown effects", str(caught.exception))

    def test_a_non_list_fails_at_load_time(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.load("  rules:\n    deny: run_command(ls)\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
