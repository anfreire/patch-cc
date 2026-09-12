"""One API over the two binary containers we support: ELF and Mach-O."""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from .. import js
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

    ``source`` is every JavaScript module the container declares, spanned as one
    :class:`patch_cc.js.Source` -- the bytes the blob carries plus their parse,
    held together so a bundle is read once however many surfaces ask it a
    question. The parse is lazy per module, so a surface that only scans for a
    literal never buys grammar, and the many-module surface is the one-module
    surface with more than one module (:meth:`patch_cc.bun.blob.Blob.js_modules`).

    There is no decoding here. The layers below work in ``bytes``
    (``blob.js_modules``, ``blob.rewrite``), tree-sitter indexes ``bytes``, and a
    ``str`` in the middle bought nothing but a second unit of offset for a splice
    to be wrong in.

    ``bytecode_size`` is the bytecode the module table names -- one module
    carried it before 2.1.242, every chunk after -- which is what ``status``
    reports.
    """

    path: str
    kind: str
    source: js.Source
    blob: blobmod.Blob
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

    parsed = blobmod.parse(blobmod.unwrap_section(section))
    modules = [(m.index, parsed.payload(m.contents)) for m in parsed.js_modules()]
    return Bundle(
        path=path,
        kind=kind,
        source=js.Source.over(modules, parsed.entry_point_id),
        blob=parsed,
        binary_size=os.path.getsize(path),
        bytecode_size=parsed.bytecode_size(),
    )


def write(bundle: Bundle, patched: js.Source, out_path: str) -> None:
    """Write the patched modules into a copy of the binary at ``out_path``.

    Only the modules whose bytes actually changed are written -- appended after
    the arena, their stale bytecode unlinked -- and every other byte of the blob
    stays where it was (:func:`patch_cc.bun.blob.rewrite`). The image is staged
    to a temp file and verified, re-extracted and compared against both what we
    meant to write and the pristine blob, *before* it is moved into place, so a
    rewrite bug fails without ever touching the live binary.
    """
    import shutil

    contents = patched.contents()
    changed = blobmod.changed_modules(bundle.blob, contents)
    section = blobmod.wrap_section(blobmod.rewrite(bundle.blob, changed))
    tmp = f"{out_path}.patch-cc.tmp"

    try:
        if bundle.kind == "elf":
            with open(bundle.path, "rb") as handle:
                raw = handle.read()
            patched_bytes = elf.write_section(raw, section)
            with open(tmp, "wb") as handle:
                handle.write(patched_bytes)
            os.chmod(tmp, os.stat(bundle.path).st_mode & 0o7777)
        else:
            shutil.copy2(bundle.path, tmp)
            macho.write_section(tmp, section)

        verify(tmp, contents, pristine=bundle.blob, edited=set(changed))
        os.replace(tmp, out_path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise


def verify(
    path: str, expected: dict[int, bytes], *, pristine: blobmod.Blob, edited: set[int]
) -> None:
    """Re-extract from a written binary and assert it is the pristine blob plus
    exactly the intended edits.

    ``expected`` is every module's intended bytes by blob index, ``pristine`` the
    blob the write started from, ``edited`` the modules whose text changed. The
    written blob is held to the pristine one byte for byte -- the whole arena in
    place, the table and the tail one shift along -- except where the edit lives:
    an edited module's ``contents`` pair naming its text in the bytes appended
    after the arena, its ``bytecode`` pair naming nothing (a stale copy would run
    instead of the edit), its hash word zeroed, and the offsets struct following
    the bytes it names. Nothing here is enumerated: a record chain this code has
    never parsed is covered by the same comparison as the one it has, which is
    what makes the check hold on a Bun release that adds one. 2.1.246 is why the
    check exists -- every module compared equal while the written binary was
    dead, because what the rewrite had lost lived in bytes no module owned.
    """
    try:
        written = read(path)
    except Exception as exc:
        raise ContainerError(f"patched binary could not be re-read: {exc}") from exc
    got = written.source.contents()
    for index, want in expected.items():
        if got.get(index) != want:
            raise ContainerError(
                "patched binary did not round-trip: extracted module "
                f"{index} differs from what we wrote"
            )

    out = written.blob
    if out.data[: pristine.table_off] != pristine.data[: pristine.table_off]:
        raise ContainerError("patched binary moved or changed a pristine arena byte")

    table = bytearray(pristine.data[pristine.table_off : pristine.table_end])
    for index in edited:
        contents = out.modules[index].contents
        if contents[0] < pristine.table_off:
            raise ContainerError(
                f"edited module {index}'s text is not in the bytes appended "
                "after the arena"
            )
        struct.pack_into(
            "<II", table, index * blobmod.RECORD + blobmod.CONTENTS, *contents
        )
        struct.pack_into("<II", table, index * blobmod.RECORD + blobmod.BYTECODE, 0, 0)
    if out.data[out.table_off : out.table_end] != bytes(table):
        raise ContainerError(
            "patched binary's module table changed beyond the edited modules' "
            "contents and bytecode pointers"
        )

    tail = bytearray(pristine.data[pristine.table_end : pristine.offsets_at])
    if pristine.hashes_at is not None:
        for index in edited:
            at = pristine.hashes_at - pristine.table_end + index * 4
            struct.pack_into("<I", tail, at, 0)
    if out.data[out.table_end : out.offsets_at] != bytes(tail):
        raise ContainerError(
            "patched binary's tail -- the record chain and compileExecArgv -- "
            "changed beyond the edited modules' hash words"
        )

    if out.flags != pristine.flags & ~blobmod.FLAG_SOURCE_TEXT_CONTIGUOUS:
        raise ContainerError(
            f"patched binary carries flags {out.flags:#x} where the pristine "
            f"blob's {pristine.flags:#x} less the contiguity bit was meant"
        )
    if out.entry_point_id != pristine.entry_point_id:
        raise ContainerError("patched binary names a different entry module")
    if out.payload(out.argv_ptr) != pristine.payload(pristine.argv_ptr):
        raise ContainerError("patched binary's compileExecArgv did not round-trip")
