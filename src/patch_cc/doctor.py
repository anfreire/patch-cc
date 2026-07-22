"""Health checks over an installed binary and the patch set.

Two different questions, deliberately kept apart:

* **status** -- is the *installed* binary patched right now? Answered by the
  manifest comment every patched bundle ends with (plus legacy fingerprints),
  and the bytecode-stripped invariant.
* **dryrun** -- would our patches still apply to *this* bundle? Answered by
  running every patch and reporting per-step hits, so a silently drifted
  matcher shows up as a concrete "reducer.message_stop missed" instead of a
  lump count.

The dry run feeds every configurable patch a synthetic configuration built
from the bundle's own discovered agents and models, so branding and the model
overrides are exercised for real instead of being exempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .bun import Bundle
from .patcher import is_patched, read_manifest
from .patches import ALL_PATCHES, Options, Outcome, Patch
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models


@dataclass(slots=True)
class Status:
    patched: bool
    bytecode_stripped: bool
    bytecode_size: int
    #: Parsed manifest for binaries patched by this tool; ``None`` when the
    #: binary is pristine or predates the manifest.
    manifest: dict | None

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
    source = bundle.source
    return Status(
        patched=is_patched(source),
        bytecode_stripped=bundle.bytecode_size == 0,
        bytecode_size=bundle.bytecode_size,
        manifest=read_manifest(source),
    )


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

    @property
    def broken(self) -> list[Patch]:
        """Patches that failed, by :attr:`Outcome.health` and nothing else.

        A second opinion on health here is how ``doctor`` once printed a red
        cross and "all patches still match" in the same report: it judged on
        counts alone, so a patch that raised half-way was red on its own line
        and absent from this list.
        """
        return [p for p, o in self.results if o.health == "broken"]


def _synthetic_options(agents: list[BuiltinAgent], models: list[str]) -> Options:
    """A configuration that forces every configurable patch to do work.

    Each discovered agent is assigned a model different from its current one,
    so the rewrite (not the already-desired no-op) is what gets tested.
    """
    overrides = {}
    for agent in agents:
        target = next((m for m in models if m != agent.effective_model), None)
        if target is not None:
            overrides[agent.name] = target
    return Options(brand="patch-cc doctor", subagent_models=overrides)


def dryrun(bundle: Bundle) -> DryRun:
    """Run every patch against the bundle without writing anything."""
    source = bundle.source
    result = DryRun(
        agents=discover_agents(source),
        models=[INHERIT, *discover_models(source)],
    )
    options = _synthetic_options(result.agents, result.models)

    for patch in ALL_PATCHES:
        _, outcome = patch.run(source, options)
        result.results.append((patch, outcome))
        if patch.anchors:
            result.anchors[patch.id] = {a: source.count(a) for a in patch.anchors}

    return result
