"""Health checks over an installed binary and the patch set.

Two different questions, deliberately kept apart:

* **status** -- is the *installed* binary patched right now? Answered by the
  manifest comment every patched bundle ends with.
* **dryrun** -- would our patches still apply to *this* bundle? Answered by
  running every patch and reporting per-step hits, so a silently drifted
  matcher shows up as a concrete "required step thinking-stop found nothing"
  instead of a lump count.

The dry run feeds every configurable patch a synthetic configuration built
from the bundle's own discovered agents and models, so branding and the model
overrides are exercised for real instead of being exempted.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
from dataclasses import dataclass, field

from . import js
from .bun import Bundle, container
from .bun.errors import BunError
from .custom_models import CustomModel
from .patcher import build_manifest, landed_ids, read_manifest
from .patches import ALL_PATCHES, Options, Outcome, Patch
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models


@dataclass(slots=True)
class Status:
    #: Parsed manifest for binaries patched by this tool; ``None`` when the
    #: binary is pristine.
    manifest: dict | None

    @property
    def patched(self) -> bool:
        return self.manifest is not None

    @property
    def patch_ids(self) -> list[str]:
        if not self.manifest:
            return []
        patches = self.manifest.get("patches")
        return (
            [p for p in patches if isinstance(p, str)]
            if isinstance(patches, list)
            else []
        )


def status(bundle: Bundle) -> Status:
    return Status(manifest=read_manifest(bundle.source))


@dataclass(slots=True)
class DryRun:
    #: Shaped exactly like :attr:`PatchReport.results`, so every surface renders
    #: a dry run and a real apply with the same loop.
    results: list[tuple[Patch, Outcome]] = field(default_factory=list)
    anchors: dict[str, dict[str, int]] = field(default_factory=dict)
    #: What discovery found in this bundle -- the agents and model aliases the
    #: override patch would offer.
    agents: list[BuiltinAgent] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    #: Patches with no surface on this build -- upstream retired the target --
    #: each with the sentence saying so (:meth:`patch_cc.patches.Patch.absent`).
    #: Kept apart from ``results`` because absence is a fact about the build,
    #: reported apart from broken (docs/CONDUCT.md): an absent patch is not run
    #: at all, exactly as the menu does not offer it, so it can neither pass nor
    #: fail and never decides :attr:`clean`.
    absent: list[tuple[Patch, str]] = field(default_factory=list)
    #: Where this build's *pristine* bundle stopped parsing, when it did --
    #: which `apply` refuses to patch at all. A rewrite that produces rubble is
    #: already reported as the patch that produced it, so this is the one
    #: parse failure no patch can be blamed for and none would survive.
    defect: js.Defect | None = None
    #: The composed result of the run and the configuration that drove it --
    #: what :func:`smoke` bakes, held so the binary it executes is the very
    #: composition the verdicts above describe rather than a second one.
    patched: js.Source | None = None
    options: Options | None = None

    @property
    def broken(self) -> list[Patch]:
        """Patches that failed, by :attr:`Outcome.health` and nothing else.

        A second opinion on health here is how a red cross and "all patches
        still match" end up in the same report: judged on counts alone, a patch
        that raised half-way is red on its own line and absent from this list.

        Kept apart from :attr:`unhealthy` because only these have an anchor
        count worth printing: a patch that found its shape and failed to rewrite
        it has already told you the anchor is there.
        """
        return [p for p, o in self.results if o.health == "broken"]

    @property
    def unhealthy(self) -> list[Patch]:
        """Every patch not fully ``ok`` -- what the verdict answers for.

        A dry run is the one place ``candidates > 0, applied == 0`` cannot mean
        "already applied": ``doctor`` runs against a **clean** bundle (the
        pristine backup when the install is patched), so the reading that makes
        a missed sub-step benign is unavailable here and what is left is a
        matcher to repair. Ending green over one is the same silence ``expect``
        exists to break -- the per-patch line would say ``~`` while the closing
        sentence says every patch still matches.

        ``apply`` judges the same outcome differently on purpose
        (:attr:`patch_cc.patcher.PatchReport.regressions` reads ``broken``
        alone): there a partial patch has still landed and still ships, and
        dropping it would cost the user a working feature over a missing
        refinement. Two questions, one health verdict, neither re-derived.
        """
        return [p for p, o in self.results if o.health != "ok"]

    @property
    def clean(self) -> bool:
        """The one green verdict: every patch fully ``ok`` *and* the bundle parses.

        The single home for the dry-run exit code, so no surface can disagree
        about it. Read in two places -- :attr:`broken` alone on one, :attr:`unhealthy`
        (partial included) and :attr:`defect` on the other -- a partially-drifted
        or unparseable build would show green ticks and exit 0 on one surface
        and red on the other.
        """
        return not self.unhealthy and self.defect is None


def _synthetic_options(agents: list[BuiltinAgent], models: list[str]) -> Options:
    """Exercise every configurable patch using values derived from the bundle.

    A synthetic external model exercises registration, shortcuts, context,
    effort limits (it lacks `max`) and routing without a network read. Subagent
    targets remain native aliases so their matcher health can be compared
    independently.
    """
    custom = [
        CustomModel(
            "doctor-9.9",
            "Doctor",
            272_000,
            efforts=("low", "medium", "high", "xhigh"),
            alias="doctor",
        )
    ]
    overrides = {
        agent.name: target
        for agent in agents
        if (target := next((m for m in models if m != agent.effective_model), None))
    }
    return Options(
        brand="patch-cc doctor",
        subagent_models=overrides,
        custom_models=custom,
        endpoint="http://127.0.0.1:8317",
        # The replace branch; the hidden form differs only in the emitted tail,
        # so one configuration holds the whole matcher in the net.
        org_label="patch-cc doctor",
    )


def dryrun(bundle: Bundle) -> DryRun:
    """Run every patch against the bundle without writing anything.

    The patches are *composed*, exactly as one pass of ``apply``'s fixpoint
    composes them, and the result is parsed. Running each patch against the
    pristine source and discarding its output would be cheaper and answer a
    question nobody asks: it cannot see `custom-models` registering the ids
    that `subagent-models` then pins -- the one ordering the playbook calls
    load-bearing -- and it cannot see the bundle at all, only counters, so a
    patch that corrupts a string reports ``candidates=2 applied=2`` and the
    rubble is thrown away on the next line.

    Anchor counts and the parse both stay measured against the *pristine*
    source. They answer "what did this build ship", which is a question about
    the build, not about what our own edits left behind -- and a rewrite that
    left rubble is already reported against the patch that made it, by the same
    gate that stops it being applied.
    """
    source = bundle.source
    result = DryRun(
        agents=discover_agents(source),
        models=[INHERIT, *discover_models(source)],
        defect=source.defect(),
    )
    options = _synthetic_options(result.agents, result.models)

    current = source
    for patch in ALL_PATCHES:
        if (why := patch.absent(source)) is not None:
            result.absent.append((patch, why))
            continue
        current, outcome = patch.run(current, options)
        result.results.append((patch, outcome))
        if patch.anchors:
            result.anchors[patch.id] = {a: source.count(a) for a in patch.anchors}

    result.patched = current
    result.options = options
    return result


@dataclass(slots=True)
class Smoke:
    """What happened when the baked binary was actually executed."""

    ok: bool
    detail: str


def smoke(bundle: Bundle, dry: DryRun, timeout: float = 60.0) -> Smoke:
    """Bake the dry run's composition into a real binary and run ``--version``.

    The matchers prove the patches still *find* their shapes; this proves the
    written container still *carries* them -- rebuilt, spliced back into the
    executable, loaded by Bun and run. Those are different checks: from 2.1.246
    (Bun 1.4.1) the record chain and the shared bytecode string table live in
    bytes no module owns, so a write that loses them round-trips every module
    byte-perfect and segfaults at launch, and no per-module comparison can miss
    loudly there. ``--version`` is the cheapest run that loads the whole graph,
    and with ``version-marker`` landed its suffix line is our own edit's
    output, so the check proves the patched code executes rather than merely
    boots around it.

    The bake goes through :func:`patch_cc.bun.container.write` -- the same
    staging, verification and (on macOS) codesign as a real apply -- into a
    temp file that is always removed.
    """
    if dry.patched is None or dry.options is None:
        return Smoke(False, "nothing composed to bake")
    landed = landed_ids(dry.results)
    patched = dry.patched.append_manifest(build_manifest(landed, dry.options))

    fd, tmp = tempfile.mkstemp(prefix="patch-cc-smoke-")
    os.close(fd)
    try:
        try:
            container.write(bundle, patched, tmp)
        except BunError as exc:
            return Smoke(False, f"bake refused: {exc}")
        try:
            proc = subprocess.run(
                [tmp, "--version"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Smoke(
                False, f"baked binary hung: --version did not return in {timeout:.0f}s"
            )
        except OSError as exc:
            # A temp dir mounted noexec, ENOMEM -- the run could not even start.
            # Still a smoke verdict, not a traceback: the question was "does the
            # baked binary run", and the honest answer is how far it got.
            return Smoke(False, f"baked binary could not be executed: {exc}")
        if proc.returncode != 0:
            reason = f"exit {proc.returncode}"
            if proc.returncode < 0:
                try:
                    reason = f"signal {signal.Signals(-proc.returncode).name}"
                except ValueError:
                    reason = f"signal {-proc.returncode}"
            noise = (proc.stderr or proc.stdout).strip().splitlines()
            cause = next((l for l in noise if "panic" in l), noise[-1] if noise else "")
            return Smoke(
                False,
                f"baked binary died on --version ({reason})"
                + (f": {cause.strip()}" if cause else ""),
            )
        output = " · ".join(l for l in proc.stdout.strip().splitlines() if l)
        marker = dry.options.version_suffix
        if "version-marker" in landed and marker not in proc.stdout:
            return Smoke(
                False,
                f"baked binary ran but never printed {marker!r} -- version-marker "
                f"landed yet its edit did not execute: {output!r}",
            )
        return Smoke(True, f"baked binary runs: {output}")
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
