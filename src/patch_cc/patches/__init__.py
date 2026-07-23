"""The patch registry.

Patches run in registration order and each sees the previous one's output.
The order below is the upstream order; do not reorder casually -- some patches
depend on regions an earlier one leaves untouched.
"""

from __future__ import annotations

from . import agents, chrome, output, streaming, thinking
from .base import (
    DEFAULT_BRAND,
    DEFAULT_SUFFIX,
    GROUP_AGENTS,
    GROUP_CHROME,
    GROUP_OUTPUT,
    GROUP_THINKING,
    SENTINEL,
    Options,
    Outcome,
    Patch,
    derived_brand,
)

ALL_PATCHES: list[Patch] = [
    *output.PATCHES,
    *thinking.PATCHES,
    *streaming.PATCHES,
    *agents.PATCHES,
    *chrome.PATCHES,
]

GROUP_ORDER = [GROUP_OUTPUT, GROUP_THINKING, GROUP_AGENTS, GROUP_CHROME]

_BY_ID = {patch.id: patch for patch in ALL_PATCHES}


def get(patch_id: str) -> Patch:
    try:
        return _BY_ID[patch_id]
    except KeyError:
        raise KeyError(f"unknown patch id: {patch_id}") from None


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
    "SENTINEL",
    "derived_brand",
    "get",
    "ids",
    "default_ids",
    "by_group",
]
