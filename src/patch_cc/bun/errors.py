"""Shared base for binary-container errors.

Kept in its own module so blob/elf/macho/pe/container can all subclass one type
without an import cycle. The CLI catches :class:`BunError` to turn any
container-layer failure into a clean message instead of a traceback.
"""

from __future__ import annotations

import os

#: How to install the native build. Three places end with this advice -- "not a
#: container we know" from :func:`container.detect`, and "no Claude found" from
#: both the CLI and the menu -- and the command differs by platform, so it
#: cannot be spelled out in each. Here because this module is the one thing
#: every container may import without a cycle.
INSTALL_HINT = (
    "irm https://claude.ai/install.ps1 | iex"
    if os.name == "nt"
    else "curl -fsSL https://claude.ai/install.sh | bash"
)


class BunError(RuntimeError):
    """Any failure reading or rewriting the Bun-embedded binary."""
