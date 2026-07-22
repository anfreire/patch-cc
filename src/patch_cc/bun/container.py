"""One API over the two binary containers we support: ELF and Mach-O."""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import blob as blobmod
from . import elf, macho
from .errors import BunError

ELF_MAGIC = b"\x7fELF"
MACHO_MAGICS = {
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",  # thin, LE
    b"\xfe\xed\xfa\xcf",
    b"\xfe\xed\xfa\xce",  # thin, BE
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",  # fat
}


class ContainerError(BunError):
    pass


def detect(path: str) -> str:
    with open(path, "rb") as handle:
        magic = handle.read(4)
    if magic == ELF_MAGIC:
        return "elf"
    if magic in MACHO_MAGICS:
        return "macho"
    raise ContainerError(
        f"{path} is neither ELF nor Mach-O. Claude Code must be the native "
        "build -- reinstall with `curl -fsSL https://claude.ai/install.sh | bash`."
    )


@dataclass(slots=True)
class Bundle:
    """The JS bundle plus everything needed to put it back.

    ``source`` is the decoded JS text -- patches operate on ``str``. The binary
    layers below work in ``bytes``; this class is the encode/decode boundary.
    """

    path: str
    kind: str
    source: str
    blob: blobmod.Blob
    header_size: int
    binary_size: int
    bytecode_size: int


def read(path: str) -> Bundle:
    kind = detect(path)
    if kind == "elf":
        with open(path, "rb") as handle:
            raw = handle.read()
        section = elf.read_section(raw)
    else:
        section = macho.read_section(path)

    payload, header_size = blobmod.unwrap_section(section)
    parsed = blobmod.parse(payload)
    return Bundle(
        path=path,
        kind=kind,
        source=parsed.entry_source().decode("utf8"),
        blob=parsed,
        header_size=header_size,
        binary_size=os.path.getsize(path),
        bytecode_size=parsed.bytecode_size(),
    )


def write(
    bundle: Bundle, source: str, out_path: str, *, drop_bytecode: bool = True
) -> None:
    """Repack ``source`` into a copy of the binary at ``out_path``.

    The patched image is staged to a temp file and verified -- re-extracted and
    compared against ``source`` -- *before* it is moved into place. A rebuild
    bug therefore fails without ever touching the live binary.
    """
    import shutil  # noqa: PLC0415

    new_blob = blobmod.rebuild(
        bundle.blob, source.encode("utf8"), drop_bytecode=drop_bytecode
    )
    section = blobmod.wrap_section(new_blob, bundle.header_size)
    tmp = f"{out_path}.patch-cc.tmp"

    try:
        if bundle.kind == "elf":
            with open(bundle.path, "rb") as handle:
                raw = handle.read()
            patched = elf.write_section(raw, section)
            with open(tmp, "wb") as handle:
                handle.write(patched)
            os.chmod(tmp, os.stat(bundle.path).st_mode & 0o7777)
        else:
            shutil.copy2(bundle.path, tmp)
            macho.write_section(tmp, section)

        verify(tmp, source)  # raises before we commit if anything is off
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def verify(path: str, expected: str) -> None:
    """Re-extract from a written binary and assert it round-trips exactly."""
    try:
        written = read(path)
    except Exception as exc:  # noqa: BLE001
        raise ContainerError(f"patched binary could not be re-read: {exc}") from exc
    if written.source != expected:
        raise ContainerError(
            "patched binary did not round-trip: extracted source differs from "
            f"what we wrote ({len(written.source):,} vs {len(expected):,} bytes)"
        )
