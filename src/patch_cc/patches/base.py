"""Patch framework: how a single rewrite of the bundle is described and run.

Matcher rules, learned the hard way upstream (see docs/PLAYBOOK.md):

* Never anchor on minified locals (``A_``, ``mET``, ``wg6``); they are
  regenerated on every upstream build.
* Anchor on string literals, ``case`` labels, prop names, or control-flow shape.
* When upstream ships several shapes, add a second narrow branch rather than
  widening one regex until it over-matches.

Porting rules, for anyone translating more of upstream's JS:

* JS ``.replace(re, fn)`` without ``/g`` replaces **once** -- that is
  ``re.sub(..., count=1)``. Python's default replaces every occurrence.
* JS ``.replace("a", "b")`` on plain strings also replaces once --
  ``str.replace(a, b, 1)``.
* Compile with :data:`re.ASCII` so ``\\w`` stays ASCII as it is in JS.
* Always pass a *function* to :func:`re.sub`; a string template would treat
  backslashes in the replacement as escapes.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

#: Prefix for every identifier we inject. These are the only reliable
#: fingerprints of our work -- value flips like ``verbose:!0`` are
#: indistinguishable from the 11 that upstream already ships.
SENTINEL = "__cc_"

# Groups, in display order.
GROUP_OUTPUT = "Output & diffs"
GROUP_THINKING = "Thinking"
GROUP_AGENTS = "Subagents"
GROUP_CHROME = "Chrome & branding"

#: Default brand: the name shown unless the user overrides it. One home so the
#: field default and the "is it rebranded?" test can never disagree.
DEFAULT_BRAND = "Claude Code"

#: Default --version marker text.
DEFAULT_SUFFIX = "(patched)"


def derived_brand() -> str:
    """The branding default: the system username, possessive.

    ``anfreire`` becomes ``anfreire's Code``. Falls back to the unbranded
    default when no username can be determined.
    """
    import getpass

    try:
        user = getpass.getuser().strip()
    except OSError:
        user = ""
    return f"{user}'s Code" if user else DEFAULT_BRAND


@dataclass(slots=True)
class Options:
    """User customisation handed to every patch."""

    brand: str = DEFAULT_BRAND
    version_suffix: str = DEFAULT_SUFFIX
    subagent_models: dict[str, str] = field(default_factory=dict)

    @property
    def rebrands(self) -> bool:
        return self.brand != DEFAULT_BRAND


@dataclass(slots=True)
class Outcome:
    """What a patch found and what it changed.

    ``candidates`` and ``applied`` describe different failures and must not be
    collapsed into one number:

    * ``candidates == 0`` -- the anchor is gone. A real regression.
    * ``candidates > 0, applied == 0`` -- shape found, rewrite was a no-op.
      Usually means already patched, not broken.
    """

    candidates: int = 0
    applied: int = 0
    notes: list[str] = field(default_factory=list)
    #: Named sub-steps, for patches built from several independent rewrites.
    steps: dict[str, "Outcome"] = field(default_factory=dict)
    #: What this sub-step's absence means (set via :meth:`step`). ``False`` --
    #: a shape some builds simply lack; ``True`` -- the patch is broken without
    #: it; a string -- name of a group of which at least one member must land.
    expect: bool | str = False
    #: Set when the patch raised. A patch that threw half-way applied whatever
    #: it had already done, so ``applied`` alone would read as success.
    error: str | None = None

    @property
    def landed(self) -> bool:
        return self.applied > 0

    @property
    def health(self) -> str:
        """``ok`` / ``partial`` / ``broken`` -- the one place that verdict lives.

        Every surface (apply report, doctor, menu) renders this same judgement,
        so none of them can disagree about whether a patch is fine.
        """
        if not self.landed or self.failures():
            return "broken"
        return "partial" if self.missed_steps() else "ok"

    def failures(self) -> list[str]:
        """Every reason this patch is broken, as sentences.

        One list so no surface can render half of them: a patch that raised and
        a patch that missed an expectation are the same verdict wearing
        different clothes, and a reader who is shown only one of them draws the
        wrong conclusion about the other.
        """
        return [*([self.error] if self.error else []), *self.unmet()]

    def note(self, message: str) -> None:
        self.notes.append(message)

    def step(self, name: str, expect: bool | str = False) -> "Outcome":
        """Get (or create) a named sub-outcome.

        A single scalar count cannot distinguish "all twelve rewrites landed"
        from "six landed and six silently drifted" -- which is exactly how
        upstream's live-thinking patch hides its own regressions. Recording each
        rewrite separately turns that into an actionable "reducer.message_stop
        missed".
        """
        sub = self.steps.setdefault(name, Outcome())
        if expect and not sub.expect:
            sub.expect = expect
        return sub

    def finalize(self) -> "Outcome":
        """Roll sub-step totals up into this outcome."""
        if self.steps:
            self.candidates += sum(s.candidates for s in self.steps.values())
            self.applied += sum(s.applied for s in self.steps.values())
        return self

    def missed_steps(self) -> list[str]:
        """Sub-steps whose shape was *found* but which failed to rewrite.

        A step that matched nothing (``candidates == 0``) is usually a shape
        that simply is not on this build -- most patches carry several
        mutually-exclusive version variants -- so it is reported separately by
        :meth:`absent_steps`, not here. A step that found candidates yet applied
        none is the genuine concern.
        """
        return [
            name
            for name, sub in self.steps.items()
            if sub.candidates > 0 and not sub.landed
        ]

    def absent_steps(self) -> list[str]:
        """Sub-steps that matched nothing on this build (informational).

        Required steps are excluded: their absence is not information, it is a
        regression, and :meth:`unmet` reports it as one.
        """
        return [
            name
            for name, sub in self.steps.items()
            if sub.candidates == 0 and sub.expect is not True
        ]

    def unmet(self) -> list[str]:
        """Expectations this run failed to meet -- each one a regression.

        Absence alone cannot be judged step by step: a missing reducer variant
        is routine while a missing group-routing rewrite silently kills the
        whole patch. The ``expect`` marks make that judgement explicit -- a
        required step must land, and each variant group must land at least one
        member -- so "green but functionally dead" cannot happen.

        Groups ask for *at least* one rather than exactly one: variants are
        alternates in practice, but a transitional build carrying two reducers
        would have both correctly patched, and that is no reason to cry wolf.
        """
        failures = []
        groups: dict[str, list[str]] = {}
        for name, sub in self.steps.items():
            if sub.expect is True:
                if not sub.landed:
                    detail = (
                        "found nothing"
                        if sub.candidates == 0
                        else f"matched {sub.candidates} but rewrote none"
                    )
                    failures.append(f"required step {name} {detail}")
            elif isinstance(sub.expect, str):
                groups.setdefault(sub.expect, []).append(name)

        for label, names in groups.items():
            if not any(self.steps[name].landed for name in names):
                failures.append(f"no {label} variant landed (tried {', '.join(names)})")
        return failures


PatchFn = Callable[[str, Options, Outcome], str]


@dataclass(slots=True)
class Patch:
    id: str
    title: str
    summary: str
    group: str
    fn: PatchFn
    default: bool = True
    #: The CLI flag that configures and auto-selects this patch (``--brand``,
    #: ``--model``, ``--suffix``); ``None`` for patches selected only by id. The
    #: single home for the patch<->flag coupling, so ``list`` and ``apply
    #: --help`` can say how an opt-in patch is turned on rather than inferring it.
    option: str | None = None
    #: Anchors to report on when this patch stops matching.
    anchors: tuple[str, ...] = ()

    def run(self, content: str, options: Options) -> tuple[str, Outcome]:
        """Run this patch, surviving its own failure.

        A raising patch keeps the *input* content -- its partial rewrites are
        discarded with the return value -- but the counts it had already
        recorded live on in ``outcome``, so the error is recorded explicitly
        rather than left to be inferred from a number that says success.
        """
        outcome = Outcome()
        try:
            content = self.fn(content, options, outcome)
        except Exception as exc:  # noqa: BLE001 - one bad patch must not abort the run
            outcome.error = f"raised {type(exc).__name__}: {exc}"
        return content, outcome.finalize()


def compile_js(pattern: str, flags: int = 0) -> re.Pattern[str]:
    """Compile a matcher with JS-compatible ``\\w`` semantics."""
    return re.compile(pattern, flags | re.ASCII)


# Matches a minified identifier, the JS `[A-Za-z_$][\w$]*` idiom.
IDENT = r"[A-Za-z_$][\w$]*"


def switch_case_end(content: str, start: int) -> int:
    """End offset of a ``switch`` arm beginning at ``start``.

    Upstream's arms end at the next ``case"`` or ``default:``, whichever comes
    first. Mirrors the scan every case-based patch does.
    """
    nxt_case = content.find('case"', start)
    nxt_default = content.find("default:", start)
    ends = [i for i in (nxt_case, nxt_default) if i != -1]
    return min(ends) if ends else len(content)


def iter_segments(content: str, needle: str):
    """Yield ``(start, end)`` for each switch arm introduced by ``needle``.

    The caller rewrites and the generator is restarted, so this is deliberately
    a simple finder rather than a stateful cursor.
    """
    index = 0
    while True:
        start = content.find(needle, index)
        if start == -1:
            return
        end = switch_case_end(content, start + len(needle))
        yield start, end
        index = start + len(needle)


def splice(content: str, start: int, end: int, replacement: str) -> str:
    return content[:start] + replacement + content[end:]
