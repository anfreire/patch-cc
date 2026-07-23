"""Remembered interactive selection.

The last selection made, so the next interactive run comes up pre-filled even
after a Claude auto-update wiped the patched binary (and its manifest) away.
Written by the interactive menu and by any ``apply`` given an explicit selection
-- a bare ``apply`` (the default set) leaves it untouched, so it never clobbers a
remembered custom pick. ``apply --from-cache`` is the non-interactive reader,
replaying that selection when explicitly asked. Deleting the file resets the
menu to defaults and leaves ``--from-cache`` with nothing to replay.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .patches import DEFAULT_BRAND, DEFAULT_SUFFIX, Options, default_ids, ids


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "patch-cc" / "selection.json"


@dataclass(slots=True)
class Selection:
    patches: list[str] = field(default_factory=default_ids)
    options: Options = field(default_factory=Options)


def load() -> Selection:
    """The last selection, or a fresh default set when none is cached or the
    file is unreadable -- the menu always gets a usable, pre-checkable set.

    Only shapes are validated here (strings in the right places); whether an
    agent or model still exists is the menu's question, answered against the
    binary it is about to patch.
    """
    try:
        data = json.loads(cache_path().read_text("utf8"))
        known = set(ids())
        brand = data.get("brand", DEFAULT_BRAND)
        suffix = data.get("suffix", DEFAULT_SUFFIX)
        models = data.get("subagent_models", {})
        return Selection(
            patches=[p for p in data.get("patches", default_ids()) if p in known],
            options=Options(
                brand=brand if isinstance(brand, str) and brand else DEFAULT_BRAND,
                version_suffix=suffix
                if isinstance(suffix, str) and suffix
                else DEFAULT_SUFFIX,
                subagent_models={
                    a: m
                    for a, m in models.items()
                    if isinstance(a, str) and isinstance(m, str)
                },
            ),
        )
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        return Selection()


def save(selection: Selection) -> None:
    """Best-effort: a cache that cannot be written just means no pre-fill next
    run -- it must never break the patch it was only trying to remember."""
    try:
        path = cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {
                    "patches": selection.patches,
                    "brand": selection.options.brand,
                    "suffix": selection.options.version_suffix,
                    "subagent_models": selection.options.subagent_models,
                },
                indent=2,
            )
            + "\n",
            "utf8",
        )
        tmp.replace(path)
    except OSError:
        pass
