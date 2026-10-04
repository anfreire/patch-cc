"""The patch registry.

Patches run in registration order and each sees the previous one's output.
The order below is the upstream order; do not reorder casually -- some patches
depend on regions an earlier one leaves untouched.

``custom_models`` runs before ``agents`` on purpose, and it is the one ordering that is
load-bearing rather than inherited. ``custom-models`` writes the chosen custom ids
into the Task tool's model enum; ``subagent-models`` reads that same enum to
decide what a subagent may be pinned to. Registering first means a pin to a
custom model is offered exactly when that model is really in the bundle -- so a
``custom-models`` the fixpoint had to drop takes its pins down with it instead of
leaving a definition pointing at a model nothing registered.
"""

from __future__ import annotations

from . import agents, chrome, custom_models, output, streaming, thinking
from .base import (
    DEFAULT_BRAND,
    DEFAULT_SUFFIX,
    GROUP_CHROME,
    GROUP_MODELS,
    GROUP_OUTPUT,
    Options,
    Outcome,
    Patch,
    derived_brand,
)

ALL_PATCHES: list[Patch] = [
    *output.PATCHES,
    *thinking.PATCHES,
    *streaming.PATCHES,
    *custom_models.PATCHES,
    *agents.PATCHES,
    *chrome.PATCHES,
]

GROUP_ORDER = [GROUP_OUTPUT, GROUP_MODELS, GROUP_CHROME]


def ids() -> list[str]:
    return [patch.id for patch in ALL_PATCHES]


def default_ids() -> list[str]:
    return [patch.id for patch in ALL_PATCHES if patch.default]


def by_group() -> dict[str, list[Patch]]:
    grouped: dict[str, list[Patch]] = {group: [] for group in GROUP_ORDER}
    for patch in ALL_PATCHES:
        grouped.setdefault(patch.group, []).append(patch)
    return grouped


__all__ = [
    "ALL_PATCHES",
    "DEFAULT_BRAND",
    "DEFAULT_SUFFIX",
    "GROUP_ORDER",
    "Options",
    "Outcome",
    "Patch",
    "by_group",
    "default_ids",
    "derived_brand",
    "ids",
]
