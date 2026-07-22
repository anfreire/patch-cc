"""Shared base for binary-container errors.

Kept in its own module so blob/elf/macho/container can all subclass one type
without an import cycle. The CLI catches :class:`BunError` to turn any
container-layer failure into a clean message instead of a traceback.
"""

from __future__ import annotations


class BunError(RuntimeError):
    """Any failure reading or rewriting the Bun-embedded binary."""
