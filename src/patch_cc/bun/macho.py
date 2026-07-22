"""Mach-O ``__BUN,__bun`` handling for macOS, via LIEF.

Unlike ELF, we use LIEF to write here. Mach-O segment growth means shifting
``__LINKEDIT`` and fixing every load command that references it -- LIEF does
that correctly, and it has no pathological relocation behaviour on Mach-O
(growth is page-aligned and bounded, so there is no size blow-up).

Any edit invalidates the code signature, and on Apple Silicon an unsigned or
stale-signature binary is killed on launch rather than merely warned about. So
the signature is removed before writing and an ad-hoc one is applied after.
"""

from __future__ import annotations

import shutil
import subprocess

from .errors import BunError

SEGMENT = "__BUN"
SECTION = "__bun"


class MachOError(BunError):
    """The Mach-O could not be read or rewritten."""


def _lief():
    try:
        import lief  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise MachOError(
            "LIEF is required to patch macOS binaries. Install it with "
            "`uv tool install patch-cc` or `pip install lief`."
        ) from exc
    lief.logging.disable()
    return lief


def _binary(path: str):
    lief = _lief()
    fat = lief.MachO.parse(path)
    if fat is None:
        raise MachOError(f"not a Mach-O binary: {path}")
    # `parse` returns a FatBinary; Claude ships thin per-arch binaries.
    binary = fat.at(0) if hasattr(fat, "at") else fat
    if binary is None:
        raise MachOError(f"no Mach-O slice found in {path}")
    return lief, binary


def read_section(path: str) -> bytes:
    _lief_mod, binary = _binary(path)
    segment = binary.get_segment(SEGMENT)
    if segment is None:
        raise MachOError(f"no {SEGMENT} segment -- not a Bun standalone binary")
    section = segment.get_section(SECTION)
    if section is None:
        raise MachOError(f"no {SEGMENT},{SECTION} section")
    return bytes(section.content)


def _page_size(lief, binary) -> int:
    try:
        return (
            16384
            if binary.header.cpu_type == lief.MachO.Header.CPU_TYPE.ARM64
            else 4096
        )
    except AttributeError:  # pragma: no cover - LIEF API drift
        return 16384


def write_section(path: str, payload: bytes, out_path: str | None = None) -> None:
    """Replace ``__BUN,__bun`` with ``payload`` and re-sign ad-hoc."""
    out_path = out_path or path
    lief, binary = _binary(path)

    if binary.has_code_signature:
        binary.remove_signature()

    segment = binary.get_segment(SEGMENT)
    if segment is None:
        raise MachOError(f"no {SEGMENT} segment")
    section = segment.get_section(SECTION)
    if section is None:
        raise MachOError(f"no {SEGMENT},{SECTION} section")

    grow = len(payload) - int(section.size)
    if grow > 0:
        page = _page_size(lief, binary)
        aligned = -(-grow // page) * page
        if not binary.extend_segment(segment, aligned):
            raise MachOError(f"failed to extend {SEGMENT} by {aligned} bytes")

    section.content = list(payload)
    section.size = len(payload)
    binary.write(out_path)
    codesign(out_path)


def codesign(path: str) -> None:
    """Apply an ad-hoc signature. Required for the binary to run on arm64."""
    if not shutil.which("codesign"):  # pragma: no cover - macOS only
        raise MachOError("`codesign` not found; cannot sign the patched binary")
    result = subprocess.run(
        ["codesign", "--sign", "-", "--force", path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:  # pragma: no cover - macOS only
        raise MachOError(f"codesign failed: {result.stderr.strip()}")


def verify_signature(path: str) -> bool:  # pragma: no cover - macOS only
    if not shutil.which("codesign"):
        return False
    return (
        subprocess.run(
            ["codesign", "--verify", "--verbose=2", path],
            capture_output=True,
        ).returncode
        == 0
    )
