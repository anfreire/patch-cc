"""Find the installed Claude Code native binary.

Only the native build is supported. The npm package no longer ships ``cli.js``
-- it is a thin wrapper that downloads the native binary -- so there is nothing
to patch in a node_modules tree anymore.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .bun import BunError, container
from .bun.errors import INSTALL_HINT

_VERSION_DIR = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

#: What the launcher is called in the directories we probe by name. Windows needs
#: the suffix: the native installer puts a plain copy at ``~/.local/bin/claude.exe``
#: there, rather than the symlink into ``versions/`` it manages elsewhere. (The
#: version directory is enumerated, not probed, so its contents are found either
#: way.)
_LAUNCHER = "claude.exe" if os.name == "nt" else "claude"


def _version_sort_key(path: Path) -> tuple[int, int, int]:
    """Sort key so 2.1.216 ranks above 2.1.9 (lexicographic order would not)."""
    match = _VERSION_DIR.match(path.name)
    if match is None:
        return (-1, -1, -1)
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


@dataclass(slots=True)
class Installation:
    """A resolved Claude install.

    ``launcher`` is what the user runs; ``binary`` is the real file we patch.
    On macOS and Linux the canonical install makes the launcher a symlink into
    ``versions/``, so the two differ and patching ``binary`` in place means the
    launcher keeps working. On Windows the installer writes a plain copy, so they
    are the same file and there is nothing to follow.
    """

    launcher: Path
    binary: Path
    version: str | None

    @property
    def is_symlinked(self) -> bool:
        return self.launcher != self.binary


def _version_of(binary: Path) -> str | None:
    name = binary.name
    return name if _VERSION_DIR.match(name) else None


def _candidates() -> list[Path]:
    home = Path.home()
    found: list[Path] = []

    on_path = shutil.which("claude")
    if on_path:
        found.append(Path(on_path))

    # The default native install location, newest version first.
    versions = home / ".local" / "share" / "claude" / "versions"
    if versions.is_dir():
        found.extend(sorted(versions.iterdir(), key=_version_sort_key, reverse=True))

    for directory in (home / ".local" / "bin", home / "bin"):
        if (launcher := directory / _LAUNCHER).exists():
            found.append(launcher)

    return found


def _resolve(path: Path) -> Installation | None:
    if not path.exists():
        return None
    real = path.resolve()
    if not real.is_file():
        return None
    return Installation(launcher=path, binary=real, version=_version_of(real))


def _is_native(binary: Path) -> bool:
    """Is this file one of the containers we can actually open?"""
    try:
        container.detect(str(binary))
    except (BunError, OSError):
        return False
    return True


def find() -> Installation | None:
    """The installed Claude -- preferring one we can actually read.

    Candidate order still decides; being a native container is only the
    tie-break. A ``claude`` on PATH that is *not* the native build -- the npm
    wrapper, a shim, a stale script -- would otherwise shadow the real binaries
    sitting one candidate later and turn every command into "reinstall the
    native build" while it is already installed.

    When nothing is native the first resolvable candidate is returned anyway, so
    the container layer gets to say precisely what the file is instead of this
    reporting nothing found at all.
    """
    fallback: Installation | None = None
    seen: set[Path] = set()
    for candidate in _candidates():
        try:
            real = candidate.resolve()
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        install = _resolve(candidate)
        if install is None:
            continue
        if _is_native(install.binary):
            return install
        fallback = fallback or install
    return fallback


def find_or_raise() -> Installation:
    install = find()
    if install is None:
        raise FileNotFoundError(
            "Could not find a Claude Code install. Install the native build with:\n"
            f"  {INSTALL_HINT}"
        )
    return install
