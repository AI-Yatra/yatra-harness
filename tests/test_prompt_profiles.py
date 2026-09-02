"""Tests for prompting practices as per-route dials.

The point of the feature is that the same instruction helps one model and
wastes a turn on another, so what these check is mostly that a dial actually
reaches the prompt, that turning it off actually removes it, and that a bad
name fails while the config is loading rather than in the middle of a session.

The delimiter tests carry the sharpest constraint. `qwen3-coder-flash` emits
`<function=list_dir></function>` as message text instead of calling anything,
so a system prompt full of angle brackets is a real hazard for that class of
model, not a stylistic preference. Markdown is the default for that reason and
a test holds it there.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from harness.config import load_config
from harness.core.errors import ConfigurationError
from harness.models import prompting
from harness.repl import prompt
from harness.repl.approvals import Mode

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "ay.yaml"


class ProfileTests(unittest.TestCase):
    def test_the_presets_are_all_valid(self) -> None:
        for name, profile in prompting.PRESETS.items():
            self.assertEqual(profile.name, name)
            profile.validated()

    def test_the_default_delimiter_is_markdown(self) -> None:
        """Angle brackets are opt in. See the module docstring."""
        for name, profile in prompting.PRESETS.items():
            if name != "xml":
                self.assertEqual(profile.delimiters, "markdown", name)

    def test_an_unknown_preset_is_refused_by_name(self) -> None:
        with self.assertRaises(KeyError) as caught:
            prompting.get("aggressive")
        self.assertIn("standard", str(caught.exception))

    def test_an_invalid_dial_value_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            prompting.with_overrides(prompting.get("standard"), delimiters="yaml")
        with self.assertRaises(ValueError):
            prompting.with_overrides(prompting.get("standard"), tool_emphasis="loud")

    def test_a_dial_can_be_moved_without_disturbing_the_others(self) -> None:
        base = prompting.get("standard")
        moved = prompting.with_overrides(base, verification=False)
        self.assertFalse(moved.verification)
        self.assertEqual(moved.tool_emphasis, base.tool_emphasis)
        self.assertEqual(moved.delimiters, base.delimiters)

    def test_headings_are_drawn_in_each_style(self) -> None:
        self.assertEqual(prompting.get("standard").heading("How to work"), "## How to work")
        self.assertEqual(prompting.get("xml").heading("How to work"), "<how_to_work>")
        plain = prompting.with_overrides(prompting.get("standard"), delimiters="plain")
        self.assertEqual(plain.heading("How to work"), "HOW TO WORK:")

    def test_only_xml_closes_a_block(self) -> None:
        self.assertEqual(prompting.get("standard").close("Environment"), "")
        self.assertEqual(prompting.get("xml").close("Environment"), "</environment>")

    def test_an_empty_block_renders_as_nothing(self) -> None:
        self.assertEqual(prompting.get("standard").block("Title", "   "), "")


class RouteResolutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG)

    def route(self, name: str):
        return self.config.router.routes[name]

    def test_an_explicit_override_beats_everything(self) -> None:
        resolved = prompting.for_route(self.route("qwen"), "bare")
        self.assertEqual(resolved.name, "bare")

    def test_a_route_that_declares_a_profile_gets_it(self) -> None:
        self.assertEqual(self.route("qwen").prompt_profile, "lean")
        self.assertEqual(prompting.for_route(self.route("qwen")).name, "lean")

    def test_a_route_that_declares_nothing_is_inferred_from_quality(self) -> None:
        """Quality is a claim the operator already made; reuse it."""
        gemini = self.route("gemini")
        self.assertEqual(gemini.prompt_profile, "")
        self.assertEqual(prompting.for_route(gemini).name, "standard")

    def test_the_quality_bands(self) -> None:
        from dataclasses import replace

        base = self.route("gemini")
        for quality, expected in ((4.5, "deep"), (4.0, "deep"), (3.0, "standard"), (2.0, "lean")):
            route = replace(base, quality=quality, prompt_profile="")
            self.assertEqual(prompting.for_route(route).name, expected, quality)

    def test_every_route_in_the_shipped_config_resolves(self) -> None:
        for name, route in self.config.router.routes.items():
            self.assertIn(prompting.for_route(route).name, prompting.PRESETS, name)


class ConfigValidationTests(unittest.TestCase):
    def load_with(self, profile_line: str):
        text = CONFIG.read_text(encoding="utf-8").replace(
            "prompt_profile: lean", profile_line, 1
        )
        path = Path(tempfile.mkdtemp()) / "c.yaml"
        path.write_text(text, encoding="utf-8")
        return load_config(path)

    def test_a_typo_fails_while_the_config_loads(self) -> None:
        """Not three turns into a session, when it is expensive to notice."""
        with self.assertRaises(ConfigurationError) as caught:
            self.load_with("prompt_profile: aggressive")
        message = str(caught.exception)
        self.assertIn("prompt_profile", message)
        self.assertIn("standard", message)

    def test_omitting_it_is_allowed(self) -> None:
        config = self.load_with("quality: 3.5")
        self.assertEqual(config.router.routes["qwen"].prompt_profile, "")

    def test_a_valid_name_survives_the_round_trip(self) -> None:
        config = self.load_with("prompt_profile: xml")
        self.assertEqual(config.router.routes["qwen"].prompt_profile, "xml")


class AssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(CONFIG)

    def render(self, profile) -> str:
        return prompt.build(self.config, ROOT, mode=Mode.SUGGEST, profile=profile)

    def test_the_core_rules_are_in_every_profile(self) -> None:
        """A dial may add to the base; nothing may remove it."""
        for name in prompting.PRESETS:
            text = self.render(prompting.get(name))
            self.assertIn("Read a file before you edit it", text, name)
            self.assertIn("Never claim a command passed", text, name)

    def test_verification_appears_only_when_it_is_on(self) -> None:
        needle = "run the project's tests or linter"
        self.assertIn(needle, self.render(prompting.get("standard")))
        self.assertNotIn(needle, self.render(prompting.get("deep")))

    def test_tool_emphasis_moves_in_both_directions(self) -> None:
        base = prompting.get("standard")
        proactive = self.render(prompting.with_overrides(base, tool_emphasis="proactive"))
        cautious = self.render(prompting.with_overrides(base, tool_emphasis="cautious"))
        neutral = self.render(prompting.with_overrides(base, tool_emphasis="neutral"))
        self.assertIn("Prefer looking to asking", proactive)
        self.assertIn("Do not edit or run anything unless", cautious)
        self.assertNotIn("Prefer looking to asking", neutral)
        self.assertNotIn("Do not edit or run anything unless", neutral)

    def test_the_bare_profile_adds_no_optional_block(self) -> None:
        text = self.render(prompting.get("bare"))
        for optional in (
            "Prefer looking to asking",
            "run the project's tests or linter",
            "ask for them together in one turn",
            "say what it told you in one line",
            "keep the plan and what is done in a file",
        ):
            self.assertNotIn(optional, text, optional)

    def test_bare_is_the_shortest_and_lean_is_not(self) -> None:
        sizes = {n: len(self.render(prompting.get(n))) for n in prompting.PRESETS}
        self.assertEqual(min(sizes, key=lambda k: sizes[k]), "bare")

    def test_the_xml_profile_tags_its_blocks(self) -> None:
        text = self.render(prompting.get("xml"))
        self.assertIn("<how_to_work>", text)
        self.assertIn("</how_to_work>", text)
        self.assertIn("<environment>", text)

    def test_no_other_profile_emits_an_angle_bracket_heading(self) -> None:
        """The qwen pseudo-tool-call failure is why this is a test."""
        for name in prompting.PRESETS:
            if name == "xml":
                continue
            text = self.render(prompting.get(name))
            self.assertNotIn("<how_to_work>", text, name)
            self.assertIn("## How to work", text, name)

    def test_operator_instructions_follow_the_delimiter_style(self) -> None:
        note = "Prefer tabs over spaces."
        markdown = prompt.build(
            self.config, ROOT, mode=Mode.SUGGEST, profile=prompting.get("standard"), extra=note
        )
        self.assertIn("## Operator instructions", markdown)
        self.assertIn(note, markdown)
        xml = prompt.build(
            self.config, ROOT, mode=Mode.SUGGEST, profile=prompting.get("xml"), extra=note
        )
        self.assertIn("<operator_instructions>", xml)

    def test_building_without_a_profile_still_works(self) -> None:
        """The default has to stand on its own; callers predate the feature."""
        text = prompt.build(self.config, ROOT, mode=Mode.SUGGEST)
        self.assertIn("## How to work", text)
        self.assertIn("Read a file before you edit it", text)

    def test_the_environment_block_is_always_present(self) -> None:
        for name in prompting.PRESETS:
            text = self.render(prompting.get(name))
            self.assertIn("Working directory:", text)
            self.assertIn("Approval mode:", text)

    def test_describe_covers_every_dial_that_can_be_set(self) -> None:
        described = {
            label.replace(" ", "_") for label, _ in prompting.describe(prompting.get("standard"))
        }
        fields = set(prompting.PromptProfile.__slots__) - {"name", "extra"}
        # `describe` renames a couple for the terminal; check nothing is missed.
        self.assertEqual(len(described), len(fields))


class DialNameTests(unittest.TestCase):
    """`/profile <dial>` must never suggest a name it would then reject."""

    def test_every_suggested_name_resolves(self) -> None:
        for name in prompting.DIALS.values():
            self.assertTrue(prompting.dial(name), name)

    def test_every_dial_targets_a_real_field(self) -> None:
        fields = set(prompting.PromptProfile.__slots__)
        for alias, field in prompting.DIALS.items():
            self.assertIn(field, fields, alias)

    def test_the_friendly_spellings_reach_the_same_field(self) -> None:
        self.assertEqual(prompting.dial("plan_first"), "reasoning_scaffold")
        self.assertEqual(prompting.dial("summarise_after_tools"), "summarize_after_tools")
        self.assertEqual(prompting.dial("summarize_after_tools"), "summarize_after_tools")

    def test_a_hyphen_or_stray_case_is_tolerated(self) -> None:
        self.assertEqual(prompting.dial("Parallel-Tools"), "parallel_tools")

    def test_an_unknown_dial_resolves_to_nothing(self) -> None:
        for bad in ("bogus", "name", "extra", ""):
            self.assertEqual(prompting.dial(bad), "", bad)

    def test_every_settable_field_is_reachable_by_some_name(self) -> None:
        """A dial with no spelling is a dial nobody can turn."""
        settable = set(prompting.PromptProfile.__slots__) - {"name", "extra"}
        self.assertEqual(set(prompting.DIALS.values()), settable)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
