"""Orchestration: read a binary, run selected patches, write it back safely."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, locate
from .bun import Bundle, container
from .patches import ALL_PATCHES, DEFAULT_SUFFIX, SENTINEL, Options, Outcome, Patch

#: Every patched bundle ends with one comment line recording exactly what was
#: applied. Comments cannot collide with code, survive re-extraction, and make
#: ``status`` a parse instead of a guess -- value-flip patches leave no other
#: fingerprint.
MANIFEST_PREFIX = "//patch-cc "

#: Fingerprints of binaries patched by versions before the manifest existed.
_LEGACY_MARKER = "(Claude Code)\\n" + DEFAULT_SUFFIX


class AlreadyPatchedError(RuntimeError):
    """The only source available is already patched; patching it would stack.

    Our edits change lengths, so a second pass over a patched bundle corrupts
    rather than updates. There is deliberately no force-override: when no
    pristine backup exists the only honest fixes are ``restore`` or a
    reinstall.
    """


@dataclass(slots=True)
class PatchReport:
    version: str | None
    kind: str
    original_size: int
    patched_size: int = 0
    results: list[tuple[Patch, Outcome]] = field(default_factory=list)
    backup: Path | None = None
    output: Path | None = None

    @property
    def landed_ids(self) -> list[str]:
        return [p.id for p, o in self.results if o.landed]

    @property
    def regressions(self) -> list[Patch]:
        """Selected patches that were expected to change something but did not."""
        return [p for p, o in self.results if not o.landed]

    @property
    def partial(self) -> list[tuple[Patch, list[str]]]:
        """Patches that landed but had some sub-step miss."""
        out = []
        for patch, outcome in self.results:
            missed = outcome.missed_steps()
            if outcome.landed and missed:
                out.append((patch, missed))
        return out


def build_manifest(landed: list[str], options: Options) -> str:
    payload: dict = {"v": 1, "tool": __version__, "patches": landed}
    if options.rebrands:
        payload["brand"] = options.brand
    if options.version_suffix != DEFAULT_SUFFIX:
        payload["suffix"] = options.version_suffix
    if options.subagent_models:
        payload["models"] = options.subagent_models
    return "\n" + MANIFEST_PREFIX + json.dumps(payload, separators=(",", ":")) + "\n"


def read_manifest(source: str) -> dict | None:
    """The applied-patch record, or ``None`` for pristine/legacy binaries."""
    start = source.rfind("\n" + MANIFEST_PREFIX)
    if start == -1:
        return None
    start += 1 + len(MANIFEST_PREFIX)
    end = source.find("\n", start)
    line = source[start:] if end == -1 else source[start:end]
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def is_patched(source: str) -> bool:
    return (
        ("\n" + MANIFEST_PREFIX) in source
        or SENTINEL in source
        or _LEGACY_MARKER in source
    )


def selected_patches(ids: list[str]) -> list[Patch]:
    """Resolve ids to patches, preserving registry (run) order."""
    wanted = set(ids)
    return [patch for patch in ALL_PATCHES if patch.id in wanted]


def run_patches(
    source: str, patches: list[Patch], options: Options
) -> tuple[str, list[tuple[Patch, Outcome]]]:
    results: list[tuple[Patch, Outcome]] = []
    current = source
    for patch in patches:
        current, outcome = patch.run(current, options)
        results.append((patch, outcome))
    return current, results


def _backup_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "patch-cc" / "backups"


def backup_path_for(install: locate.Installation) -> Path:
    """The single source of truth for where a binary's backup lives.

    Canonical native installs are version-named, giving a clean
    ``<name>.<version>.orig``. When the name is not a version we cannot tell two
    unrelated ``claude`` binaries apart by name alone, so a short hash of the
    absolute path is mixed in to keep their backups distinct.
    """
    root = _backup_dir()
    if install.version:
        stem = f"{install.binary.name}.{install.version}"
    else:
        digest = hashlib.sha256(str(install.binary.resolve()).encode()).hexdigest()[:8]
        stem = f"{install.binary.name}.unknown-{digest}"
    return root / f"{stem}.orig"


def read_pristine(install: locate.Installation) -> Bundle:
    """The bundle patching starts from: the backup when one exists.

    Patching never stacks edits on edits -- each apply begins at this pristine
    source, so the selected set is always exactly what ends up in the binary.
    """
    backup = backup_path_for(install)
    return container.read(str(backup if backup.exists() else install.binary))


def _backup(install: locate.Installation, *, pristine: bool) -> Path | None:
    """Record the pristine original once, so ``restore`` is a plain copy back.

    Only ever captures a binary that is actually unpatched: backing up an
    already-marked binary would enshrine a poisoned "original" that ``restore``
    would later hand back as clean.
    """
    dest = backup_path_for(install)
    if dest.exists():
        return dest
    if not pristine:
        return None
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(install.binary, dest)
    return dest


def patch_installation(
    install: locate.Installation,
    selected: list[str],
    options: Options,
    *,
    bundle: Bundle | None = None,
    out_path: Path | None = None,
    make_backup: bool = True,
) -> PatchReport:
    """Patch ``install`` (or write to ``out_path``) with the ``selected`` patches.

    Patching always starts from a pristine source (:func:`read_pristine`), so
    re-applying replaces the previous patch set instead of stacking on it.
    ``bundle`` may carry that already-read source (the CLI reads it for
    validation first).
    """
    source = bundle if bundle is not None else read_pristine(install)
    if is_patched(source.source):
        raise AlreadyPatchedError(
            f"{install.binary} is already patched and no pristine backup exists, "
            "so there is nothing clean to patch from. Run `patch-cc restore`, "
            "or reinstall Claude to get a clean binary."
        )

    patches = selected_patches(selected)
    patched_source, results = run_patches(source.source, patches, options)

    report = PatchReport(
        version=install.version,
        kind=source.kind,
        original_size=source.binary_size,
        results=results,
    )

    landed = report.landed_ids
    if not landed:
        # Nothing changed; writing would only strip bytecode for no benefit.
        return report

    patched_source += build_manifest(landed, options)

    target = out_path or install.binary
    if make_backup and out_path is None:
        report.backup = _backup(install, pristine=not is_patched(source.source))

    container.write(source, patched_source, str(target))
    report.output = Path(target)
    report.patched_size = Path(target).stat().st_size
    return report


def clean_source_path(install: locate.Installation) -> Path | None:
    """A binary whose bundle is guaranteed unpatched, for matcher-health tests.

    If the installed binary is already patched, our own edits have removed the
    anchors the matchers look for, so a dry-run against it conflates
    "already applied" with "anchor gone". The pristine backup is the honest
    thing to test against.
    """
    backup = backup_path_for(install)
    return backup if backup.exists() else None


def restore(install: locate.Installation) -> Path:
    """Copy the pristine backup back over the installed binary."""
    backup = backup_path_for(install)
    if not backup.exists():
        raise FileNotFoundError(
            f"No backup found for {install.binary.name} "
            f"{install.version or '(unknown version)'} at {backup}. "
            "If Claude auto-updated, the original for this version was never saved -- "
            "reinstall to get a clean binary."
        )
    # A full-file copy-back, so it works for both ELF and Mach-O.
    from .bun.elf import atomic_write  # noqa: PLC0415

    atomic_write(str(install.binary), backup.read_bytes(), mode_from=str(backup))
    return install.binary
