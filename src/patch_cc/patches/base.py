"""Patch framework: how a single rewrite of the bundle is described and run.

One rule locates everything here (docs/PLAYBOOK.md):

    Find by the name upstream's authors wrote. Edit the grammar node.
    Never describe the syntax in between.

The names -- property names, ``case`` labels, string literals, the API's own
vocabulary -- are what a build keeps. The grammar is what gives an edit its
boundaries. Everything else in a minified bundle (local identifiers, statement
order, comma-fusion versus separate statements, braces around a single
statement, whether a helper was extracted) is regenerated on every build, and
:mod:`patch_cc.js` makes it invisible rather than obligatory.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..js import Source

if TYPE_CHECKING:
    from ..custom_models import CustomModel

# Groups, in display order: what renders, what runs (and how hard), how it is
# dressed. Generic on purpose -- a group named after a feature (Thinking,
# Subagents, Models) fits exactly that feature, and the next patch that is
# about behaviour rather than a surface has no home.
GROUP_OUTPUT = "Output & display"
GROUP_MODELS = "Models & effort"
GROUP_CHROME = "Chrome & branding"

#: Default brand: the name shown unless the user overrides it. One home so the
#: field default and the "is it rebranded?" test can never disagree.
DEFAULT_BRAND = "Claude Code"

#: Default --version marker text.
DEFAULT_SUFFIX = "(patched)"


def derived_brand() -> str:
    """The branding default: the system username, possessive.

    ``anfreire`` becomes ``anfreire's Code``. Falls back to the unbranded
    default when no username can be determined -- however it fails. What
    :func:`getpass.getuser` raises is a Python-version detail (``OSError`` from
    3.13, ``KeyError``/``ImportError`` before it, when a container has no passwd
    entry and no ``LOGNAME``), and none of them change the answer: nobody to
    name, so no name. Catching the 3.13 spelling alone lets a traceback out of
    `apply` -- which carries branding by default -- on the two older versions
    this package supports.
    """
    import getpass

    try:
        user = getpass.getuser().strip()
    except Exception:  # noqa: BLE001 - however it fails, the answer is the same
        user = ""
    return f"{user}'s Code" if user else DEFAULT_BRAND


@dataclass(slots=True)
class Options:
    """User customisation handed to every patch."""

    brand: str = DEFAULT_BRAND
    version_suffix: str = DEFAULT_SUFFIX
    subagent_models: dict[str, str] = field(default_factory=dict)
    custom_models: list[CustomModel] = field(default_factory=list)
    endpoint: str = ""
    #: The welcome screen's org segment (for personal claude.ai accounts,
    #: upstream shows the account email there). Empty is a *value* -- hide the
    #: segment -- not "unset": whether the patch acts at all is the selection's
    #: question, never this field's.
    org_label: str = ""

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

    Both are read for the verdict (:attr:`landed`), because a report that keeps
    two numbers and judges on one has the second for decoration.
    """

    candidates: int = 0
    applied: int = 0
    notes: list[str] = field(default_factory=list)
    #: Named sub-steps, for patches built from several independent rewrites.
    steps: dict[str, Outcome] = field(default_factory=dict)
    #: What this sub-step's absence means (set via :meth:`declare`). ``False`` --
    #: a shape some builds simply lack; ``True`` -- the patch is broken without
    #: it.
    expect: bool = False
    #: Set when the patch raised. A patch that threw half-way applied whatever
    #: it had already done, so ``applied`` alone would read as success.
    error: str | None = None

    @property
    def landed(self) -> bool:
        """Did this find what it was looking for *and* change something?

        Both counts, one verdict, because either alone is satisfiable by a
        rewrite that achieved nothing. ``applied`` is what a matcher reports
        about itself; ``candidates`` is what the *durable* witness reports --
        the header name behind the read, the interpolation behind the
        conditional -- and a patch that pays for one is paying to be told when
        the witness goes. Judged on ``applied`` alone, `thinking-summaries`
        would stay green with its header renamed: every read rewritten, and
        nothing left that those reads fed.
        """
        return self.candidates > 0 and self.applied > 0

    @property
    def health(self) -> str:
        """``ok`` / ``partial`` / ``broken`` -- the one place that verdict lives.

        Every surface (apply report, doctor, menu) renders this same judgement,
        so none of them can disagree about whether a patch is fine.

        ``partial`` is *some* of the work, not all of it: a sub-step found its
        shape and failed to rewrite it (:meth:`missed_steps`), or the patch's own
        rewrites covered fewer sites than it found (``applied < candidates``).
        Without that middle row a patch with two welcome lines and one of them
        reshaped reads ``cand=2 applied=1`` and calls itself ``ok`` -- one line
        unpatched under a green tick. A rewrite that *undercounts* its witness
        (``applied > candidates``, the header behind more reads than headers)
        is not drift and stays ``ok``.
        """
        if not self.landed or self.failures():
            return "broken"
        if self.missed_steps() or self.applied < self.candidates:
            return "partial"
        return "ok"

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

    def declare(
        self, required: tuple[str, ...] = (), optional: tuple[str, ...] = ()
    ) -> None:
        """Create sub-steps, before any of them does its work.

        Declaring is the only way a step comes to exist (:meth:`step` only
        retrieves), which makes "declare an expectation before the work" the
        API's shape instead of each patch's discipline: a code path that never
        runs leaves a required step at 0/0 with a verdict to fail, where a step
        created by its own success could never report its own absence -- a
        lazily-created `badge` step would leave `branding`'s badge unrenamed
        under exactly that silence. Conditional work declares under the same
        condition it runs (`context`), and a name resolved from the bundle is
        declared the moment it resolves (`bypass:<agent>`).
        """
        for name in required:
            self.steps.setdefault(name, Outcome()).expect = True
        for name in optional:
            self.steps.setdefault(name, Outcome())

    def step(self, name: str) -> Outcome:
        """A declared sub-step, to record work against.

        A single scalar count cannot distinguish "all five rewrites landed"
        from "three landed and two silently drifted". Recording each rewrite
        separately turns that into an actionable "required step thinking-stop
        found nothing".

        Retrieval only: a name nobody declared is a programming error and
        raises, which :meth:`Patch.run` reports as the patch broken -- loud,
        never a silently-optional step minted by a typo.
        """
        if name not in self.steps:
            raise RuntimeError(f"step {name!r} was never declared")
        return self.steps[name]

    def finalize(self) -> Outcome:
        """Roll sub-step totals up into this outcome."""
        if self.steps:
            self.candidates += sum(s.candidates for s in self.steps.values())
            self.applied += sum(s.applied for s in self.steps.values())
        return self

    def missed_steps(self) -> list[str]:
        """Sub-steps whose shape was *found* but which some site failed to rewrite.

        A step that matched nothing (``candidates == 0``) is usually a shape
        that simply is not on this build, so it is reported separately by
        :meth:`absent_steps`, not here. A step that found more candidates than it
        rewrote is the genuine concern: that covers a step that rewrote *none*
        (``applied == 0``) and one that rewrote *some* (``0 < applied <
        candidates``, partial drift) alike, where reading only ``landed``
        (``applied > 0``) would call the partial case fully applied.
        """
        return [
            name for name, sub in self.steps.items() if sub.candidates > sub.applied
        ]

    def absent_steps(self) -> list[str]:
        """Sub-steps that matched nothing on this build (informational).

        Required steps are excluded: their absence is not information, it is a
        regression, and :meth:`unmet` reports it as one.
        """
        return [
            name
            for name, sub in self.steps.items()
            if sub.candidates == 0 and not sub.expect
        ]

    def unmet(self) -> list[str]:
        """Expectations this run failed to meet -- each one a regression.

        Absence alone cannot be judged step by step: a reducer arm this build
        folded away is routine while a missing group-routing rewrite silently
        kills the whole patch. The ``expect`` marks make that judgement
        explicit -- a required step must land -- so "green but functionally
        dead" cannot happen.
        """
        failures = []
        for name, sub in self.steps.items():
            if sub.expect and not sub.landed:
                detail = (
                    "found nothing"
                    if sub.candidates == 0
                    else f"matched {sub.candidates} but rewrote none"
                )
                failures.append(f"required step {name} {detail}")
        return failures


PatchFn = Callable[[Source, Options, Outcome], Source]


@dataclass(frozen=True, slots=True)
class Setting:
    """Where a configurable patch's chosen value lives *outside* the binary.

    A configurable patch carries a fact that has to survive in two stores with
    different jobs: the **manifest** records what is *in the binary* (the
    menu's "pending apply" and its seed when nothing is saved), the **cache**
    records what was *asked for* (what ``apply`` bakes and the menu edits).
    Both keep it under the one ``key``, usually as the same value; a custom
    model's reported window choices are the one thing the cache keeps and the
    manifest does not, which is why each store has its own pair of hooks.

    The patch declares all of it here, once, and the two stores read it -- so
    neither store can spell a fact the other does not, and nothing fails
    quietly when a home is missed. Rendering (the report line, the list hint,
    the menu row) is left to each surface: they differ on purpose -- one shows
    ``a=b, c=d``, another ``2 overrides`` -- and are display, where a slip is
    seen rather than silently stored.

    Every hook takes/updates an :class:`Options`, so the manifest and the cache
    round-trip through the same field the patch already reads at bake time.
    """

    #: The key this value takes in the manifest JSON and in the cache (`brand`,
    #: `suffix`, `org_label`, `subagent_models`, `custom_models`).
    key: str
    #: Is there a value worth recording? (`brand` differs from the default, the
    #: override dict is non-empty, ...) -- gates both stores.
    recorded: Callable[[Options], bool]
    #: The value to write under ``key`` in the manifest.
    to_manifest: Callable[[Options], object]
    #: Apply a manifest value back onto an ``Options`` (menu pre-select).
    from_manifest: Callable[[Options, object], None]
    #: The value to write under ``key`` in the cache.
    to_cache: Callable[[Options], object]
    #: Apply a cache value back onto an ``Options`` (replay/pre-fill).
    from_cache: Callable[[Options, object], None]


def string_setting(
    key: str, field: str, default: str, *, always: bool = False
) -> Setting:
    """A :class:`Setting` for a plain string value.

    Covers the three chrome settings, which differ only in their key and
    default: ``brand``, ``version_suffix`` (``suffix``) and ``org_label``.
    ``always`` records even the empty value -- an emptied ``org-label`` *means*
    "hide the segment", so unlike a blank brand it is a real recorded choice.
    """

    def parse(value: object) -> str:
        return value if isinstance(value, str) and (value or always) else default

    def write(options: Options) -> str:
        return getattr(options, field)

    def read(options: Options, value: object) -> None:
        setattr(options, field, parse(value))

    return Setting(
        key=key,
        recorded=lambda o: always or getattr(o, field) != default,
        to_manifest=write,
        from_manifest=read,
        to_cache=write,
        from_cache=read,
    )


@dataclass(slots=True)
class Patch:
    id: str
    title: str
    group: str
    fn: PatchFn
    default: bool = True
    #: Anchors to report on when this patch stops matching.
    anchors: tuple[str, ...] = ()
    #: How this patch's configurable value is stored, for the patches that carry
    #: one; ``None`` for a plain toggle. The one home for its manifest and cache
    #: keys, so the two stores cannot drift and adding a configurable patch is
    #: one declaration rather than eight edits.
    setting: Setting | None = None
    #: Whether this patch still has a surface on a given bundle: the sentence
    #: explaining its absence, or ``None`` while the surface is there. Discovery's
    #: question asked one level up -- the binary in hand says what can be offered
    #: -- for a patch whose whole target upstream may retire outright (org-label's
    #: welcome segment left in 2.1.246). The menu leaves the row out, a saved
    #: replay skips it with a warning, and `doctor` reports it apart from
    #: broken: a build without the surface is a fact about the build, not a
    #: regression in a matcher. The field left ``None`` means the surface is
    #: every build's.
    absence: Callable[[Source], str | None] | None = None

    def absent(self, source: Source) -> str | None:
        """Why this patch has no surface on this bundle -- ``None`` while it has one."""
        return self.absence(source) if self.absence is not None else None

    def run(self, source: Source, options: Options) -> tuple[Source, Outcome]:
        """Run this patch, surviving its own failure.

        A raising patch keeps the *input* source -- a `Source` is never mutated,
        so partial rewrites are discarded with the return value -- but the
        counts it had already recorded live on in ``outcome``, so the error is
        recorded explicitly rather than left to be inferred from a number that
        says success.

        A rewrite that leaves the bundle unparseable raises out of
        :meth:`Source.apply` and lands here, which is the whole reason the gate
        sits at the edit rather than at the end of the run: the patch that
        produced rubble is named, dropped from the set, and the rest still
        apply.
        """
        outcome = Outcome()
        try:
            source = self.fn(source, options, outcome)
        except Exception as exc:  # noqa: BLE001 - one bad patch must not abort the run
            outcome.error = f"raised {type(exc).__name__}: {exc}"
        return source, outcome.finalize()


def js_string(value: str) -> str:
    """``value`` as a complete, quoted JS string literal -- never hand-escaped.

    JSON's string grammar is a subset of JavaScript's, so :func:`json.dumps` is
    already the correct encoder: it closes over quotes, backslashes, *control
    characters*, and non-ASCII (as ``\\uXXXX``). Escaping by hand covers the
    characters you thought of, and a brand carrying a newline (``Ada\\nOwned``,
    hand-edited into the saved file) would put a raw line terminator inside a
    double-quoted literal -- a bundle that no longer parses, which nothing but
    the grammar catches: every step counts as applied, and the write verifier
    re-extracts what was written and compares it to what was meant, so it
    agrees the invalid source is correct. The binary dies at launch with
    ``SyntaxError: Unexpected EOF``.

    Build the whole Python string first and quote it once here; do not
    interpolate into a literal you wrote yourself.
    """
    return json.dumps(value)
