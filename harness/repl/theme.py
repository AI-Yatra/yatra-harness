"""One palette and one layout grid, so the session looks like one program.

Colour here is information, not decoration, so there are five roles and no
more. Anything that does not carry meaning is left at the terminal's own
foreground, which is the only colour guaranteed to be readable.

**The colours are measured, not chosen by eye.** A terminal's background is
unknown and unknowable: it may be white, black, or Solarized Dark, and the
program cannot ask. So each role's colour was picked by maximising its *worst*
WCAG contrast ratio across all three. The arithmetic ceiling for a single
colour that must work on both white and black is sqrt(21), about 4.58, reached
only at relative luminance 0.179; every colour below is within 0.9 of that
ceiling. The numbers are in the comments and reproducible from the constants.

Three findings from terminal practice shaped the rest.

**SGR 2 (`dim`/faint) is not used anywhere.** Support is inconsistent: some
terminals ignore it, leaving no hierarchy at all, and others render it so faint
it is unreadable on a dark background. The old renderer used it for the
majority of its output, which meant most of the interface was at the mercy of a
code that had no defined appearance. A measured grey replaces it.

**The 16 ANSI colours are not used either.** They have no standard: each
terminal theme picks its own, so `blue` may be anything and is famously
illegible on black in several popular themes. The 256-colour cube is fixed, so
a chosen colour is the colour that appears.

**Nothing depends on colour alone.** Piped output, `NO_COLOR`, and a dumb
terminal all lose every code here, so the layout below has to carry the
structure by itself. That is what the grid is for.
"""

from __future__ import annotations

from dataclasses import dataclass

RESET = "\033[0m"

#: The gutter every line shares. Marks live in it; content starts after it.
#: Two columns, because one reads as a typo and four wastes a narrow terminal.
GUTTER = 2

#: Continuation and nested detail sit one gutter deeper than their parent.
INDENT = GUTTER * 2

#: Prose is not run to the edge of the window. A line that ends flush against
#: the frame is measurably harder to track back from, and on a wide terminal an
#: unbounded measure is worse still: past roughly 100 columns the eye loses the
#: start of the next line. Both bounds are applied in `Console.width`.
RIGHT_MARGIN = 2


@dataclass(frozen=True, slots=True)
class Theme:
    """Five roles. Each value is an SGR parameter list, not a colour name.

    Named for what the thing *is*, never for how it looks, so a role can be
    retuned in one place without every call site becoming a lie.
    """

    #: The agent's own marks and anything the operator can act on: the tool
    #: bullet, the prompt caret, the numbered keys in a permission question.
    #: 256-colour 32, #0087d7. Worst-case contrast 3.86.
    #:
    #: Blue because the wordmark is blue, and because a single accent used
    #: consistently is what makes a program look like one program. It is a
    #: *mid* blue for a reason: the light blues in the banner ramp measure
    #: 1.59 to 2.45 against white and would be nearly invisible there.
    accent: str = "38;5;32"

    #: Secondary text: arguments, counts, paths, hints, elapsed times. Present
    #: when looked for, quiet when not. 244, #808080. Worst case 3.80.
    muted: str = "38;5;244"

    #: A thing that worked. 65, #5f875f. Worst case 3.66.
    success: str = "38;5;65"

    #: A thing that did not, or a state that ought to worry the operator: a
    #: denied call, a missing key, a session running with every approval
    #: waived. One colour for "wrong or dangerous" rather than two, because a
    #: separate caution role would have to be yellow, and yellow is the worst
    #: colour on this axis -- it measures under 1.6 against a white terminal.
    #: 167, #d75f5f. Worst case 3.69.
    #:
    #: Paired with `success` at the same saturation so a diff does not read as
    #: one loud colour against one quiet one.
    failure: str = "38;5;167"

    #: Emphasis without colour, so it survives a monochrome terminal. This is
    #: the only role that still works when every colour is stripped, which is
    #: why headings and tool names use it rather than a hue.
    strong: str = "1"


#: The one instance. A dataclass rather than module constants so an operator
#: theme can be introduced later without changing a single call site.
THEME = Theme()
