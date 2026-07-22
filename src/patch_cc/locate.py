"""Find the installed Claude Code native binary.

Only the native build is supported. The npm package no longer ships ``cli.js``
-- it is a thin wrapper that downloads the native binary -- so there is nothing
to patch in a node_modules tree anymore.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_VERSION_DIR = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


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

    ``launcher`` is what the user runs (often a symlink); ``binary`` is the real
    file we patch. When Claude is installed the canonical way they differ, and
    patching ``binary`` in place means the launcher keeps working.
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

    for extra in (home / ".local" / "bin" / "claude", home / "bin" / "claude"):
        if extra.exists():
            found.append(extra)

    return found


def _resolve(path: Path) -> Installation | None:
    if not path.exists():
        return None
    real = path.resolve()
    if not real.is_file():
        return None
    return Installation(launcher=path, binary=real, version=_version_of(real))


def find() -> Installation | None:
    """Return the first resolvable installation, or ``None``."""
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
        if install:
            return install
    return None


def find_or_raise() -> Installation:
    install = find()
    if install is None:
        raise FileNotFoundError(
            "Could not find a Claude Code install. Install the native build with:\n"
            "  curl -fsSL https://claude.ai/install.sh | bash"
        )
    return install


def all_versions() -> list[Installation]:
    """Every version binary found under the native versions directory."""
    versions = Path.home() / ".local" / "share" / "claude" / "versions"
    if not versions.is_dir():
        return []
    out: list[Installation] = []
    for entry in sorted(versions.iterdir(), key=_version_sort_key, reverse=True):
        if entry.is_file():
            out.append(
                Installation(launcher=entry, binary=entry, version=_version_of(entry))
            )
    return out
