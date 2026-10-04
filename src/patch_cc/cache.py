"""The saved selection: the one input ``apply`` bakes, and what the menu edits.

The file keeps every patch's setting, on or off; :meth:`Selection.active`
derives the patch input from it. The binary's manifest records what is
*applied* and seeds the selection only while nothing has been saved. Both the
menu and ``apply`` read it through :func:`seed` and write it back after a
successful bake, so the file always describes the last bake and the two can
never disagree about what the next one would do.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field, replace
from pathlib import Path

from .custom_models import model_names
from .patches import ALL_PATCHES, Options, default_ids, derived_brand, ids


def cache_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME")
    root = Path(base) if base else Path.home() / ".cache"
    return root / "patch-cc" / "selection.json"


@dataclass(slots=True)
class Selection:
    patches: list[str] = field(default_factory=default_ids)
    options: Options = field(default_factory=lambda: Options(brand=derived_brand()))
    #: Patch ids the cache named that this tool no longer has, dropped from
    #: :attr:`patches` while loading. Transient (never saved), so whoever reads
    #: the file can say what was left out -- a replay before it applies fewer
    #: patches than were saved, the menu as it opens -- and the next save
    #: writes the file without them.
    dropped_patches: list[str] = field(default_factory=list)

    def payload(self) -> dict:
        """All saved preferences, including disabled values."""
        data: dict[str, object] = {"patches": self.patches}
        for patch in ALL_PATCHES:
            if patch.setting is not None:
                data[patch.setting.key] = patch.setting.to_cache(self.options)
        return data

    def active(self) -> Selection:
        """The patch input, derived without changing the saved preferences."""
        selected = list(self.patches)
        options = replace(self.options)
        unrouted: set[str] = set()
        if "custom-models" not in selected:
            options.custom_models = []
            unrouted = set(model_names(self.options.custom_models))
        options.subagent_models = {
            agent: model
            for agent, model in self.options.subagent_models.items()
            if "subagent-models" in selected and model not in unrouted
        }
        empty = {
            "custom-models": not options.custom_models,
            "subagent-models": not options.subagent_models,
            "branding": not options.rebrands,
        }
        return Selection(
            [pid for pid in selected if not empty.get(pid)],
            options,
            list(self.dropped_patches),
        )


def from_manifest(manifest: dict | None) -> Selection:
    if manifest is None:
        return Selection()
    options = Options()
    for patch in ALL_PATCHES:
        if patch.setting is not None and patch.setting.key in manifest:
            patch.setting.from_manifest(options, manifest[patch.setting.key])
    return Selection(
        [pid for pid in manifest.get("patches", []) if pid in ids()], options
    )


def seed(manifest: dict | None) -> tuple[Selection, str | None]:
    """Saved preferences first, else the installed state -- and why, when the
    saved file was passed over.

    An unreadable file is reported, not raised: the menu wants to open anyway
    (on the binary's own state, with the sentence shown) and ``apply``, which
    *acts* on the selection, wants to refuse. The substitution belongs to the
    caller that wants it, so both read the same answer and decide for themselves.
    """
    saved = load()
    if saved is not None:
        return saved, None
    note = (
        f"The saved selection at {cache_path()} could not be read."
        if cache_path().exists()
        else None
    )
    return from_manifest(manifest), note


def pending(selection: Selection, manifest: dict | None) -> bool:
    """Would applying change the binary? What it would bake, against what the
    binary records -- the manifest as carried, not re-read through this tool's
    vocabulary. A manifest may name a patch this tool has no id for, and that
    patch is still *in* the binary; re-read through ``from_manifest`` it would
    be dropped, and the header would say "matches" over a binary that applying
    would change.
    """
    from .patcher import manifest_payload

    active = selection.active()
    would = manifest_payload(sorted(active.patches), active.options)
    carried = dict(manifest) if manifest is not None else {"patches": []}
    carried["patches"] = sorted(
        pid for pid in carried.get("patches", []) if isinstance(pid, str)
    )
    return {k: v for k, v in would.items() if k not in ("v", "tool")} != {
        k: v for k, v in carried.items() if k not in ("v", "tool")
    }


def load() -> Selection | None:
    """The saved selection, or ``None`` for an absent or invalid file."""
    try:
        data = json.loads(cache_path().read_text("utf8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None  # a cache that is not a JSON object holds no selection
    known = set(ids())
    saved = data.get("patches", default_ids())
    if not isinstance(saved, list) or any(not isinstance(p, str) for p in saved):
        return None
    # Each configurable patch reads its own value back off the cache dict, under
    # the key it also writes (`Patch.setting`), with the shape validation the
    # setting owns -- so the cache and the manifest cannot spell the same fact
    # two ways.
    options = Options()
    try:
        for patch in ALL_PATCHES:
            if patch.setting is not None:
                patch.setting.from_cache(options, data.get(patch.setting.key))
    except ValueError:
        return None
    return Selection(
        patches=[p for p in saved if p in known],
        dropped_patches=[p for p in saved if p not in known],
        options=options,
    )


def save(selection: Selection) -> None:
    """Persist the complete preference set or report the write failure."""
    _write(selection.payload())


def _write(data: dict[str, object]) -> None:
    path = cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".selection-", dir=path.parent)
    tmp = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)
