"""Read and rewrite the Bun standalone-executable blob.

Layout of the blob (little-endian throughout)::

    [ arena: name / contents / sourcemap / bytecode / ... payloads, and      ]
    [        whatever else the builder placed there                          ]
    [ module table: N records of `RECORD` bytes                              ]
    [ tail: flag-gated records, compileExecArgv, padding                     ]
    [ 32-byte offsets struct                                                 ]
    [ 15-byte "\\n---- Bun! ----\\n" trailer                                  ]

Every pointer is a ``(u32 offset, u32 length)`` pair relative to the start of
the blob, and Bun reads a payload *through* its pointer; bytes nothing points
at are ignored. That is the loader's contract, and it is the only thing the
rewrite rides. It says nothing about how the builder lays the arena out or
which records follow the table -- the half of the format that changes with Bun
(docs/INTERNALS.md).

So the rewrite never moves a pristine byte. An edited module's text is appended
after the arena; its ``contents`` pair is aimed at the text and its ``bytecode``
pair at nothing; the table and everything after it are copied verbatim, one
shift further along. A record this code has never heard of comes through the
same copy and still points where it did, because what it points at did not
move. What has to be known is exactly what is touched: the offsets struct, two
pairs and the loader byte of a module record, and the one record that describes
a module's text -- its hash, first after the table when the flag says there is
one.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import BunError

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32
#: Bytecode, and the payloads JSC reads in place, sit on a 128-byte phase of the
#: blob offset (the section's 8-byte size prefix makes that file alignment). The
#: table and everything after it are shifted by a multiple of this, so no phase
#: changes; the arena is never shifted at all.
ALIGN = 128
#: A module record: six ``(u32 offset, u32 length)`` pairs -- name, contents,
#: sourcemap, bytecode, moduleInfo, bytecodeOriginPath -- then four ``u8``
#: flags: encoding, loader, moduleFormat, side. Proved against the artifact by
#: :func:`parse`: the table is a whole number of records and every pair points
#: into the arena, which a record of another size cannot satisfy by accident.
RECORD = 52
CONTENTS = 1 * 8
BYTECODE = 3 * 8
LOADER = RECORD - 4 + 1
#: The two ``flags`` bits the rewrite has an opinion on. Every other bit -- the
#: runtime switches, the records after the table, whatever Bun adds -- travels
#: verbatim, whether or not this code has heard of it.
FLAG_SOURCE_TEXT_CONTIGUOUS = 1 << 4
FLAG_HAS_SOURCE_HASHES = 1 << 5


class BlobError(BunError):
    """The section is not a Bun blob this code can read or rewrite."""


@dataclass(slots=True)
class Module:
    index: int
    contents: tuple[int, int]
    bytecode: tuple[int, int]
    loader: int


@dataclass(slots=True)
class Blob:
    data: bytes
    modules: list[Module]
    table_off: int
    table_len: int
    argv_ptr: tuple[int, int]
    entry_point_id: int
    flags: int
    offsets_at: int

    @property
    def table_end(self) -> int:
        return self.table_off + self.table_len

    @property
    def hashes_at(self) -> int | None:
        """Where the per-module source-hash words start, or ``None`` if none.

        ``[u32; modules]`` of each text's WTF hash, first after the module table
        when :data:`FLAG_HAS_SOURCE_HASHES` says so. Bun keys the text under it,
        so an edited module's word is set to 0 -- upstream's own "none, compute
        it" -- rather than left describing the pristine text.
        """
        return self.table_end if self.flags & FLAG_HAS_SOURCE_HASHES else None

    def payload(self, rng: tuple[int, int]) -> bytes:
        off, length = rng
        return self.data[off : off + length]

    def entry_module(self) -> Module:
        """The module Bun runs, as the container declares it (``entry_point_id``)
        rather than by any name it has been seen under (docs/PLAYBOOK.md,
        *Discovery instead of hardcoding*)."""
        return self.modules[self.entry_point_id]

    def js_modules(self) -> list[Module]:
        """Every module Bun loads the way it loads the entrypoint: the app's JS.

        A module's ``loader`` byte says how Bun reads it, and the entry is by
        definition the JS Bun runs, so its loader *is* the JS loader. The
        assets (native addons, bundled minified libraries, embedded templates)
        carry other loaders and are opaque bytes. Pre-split this is the
        one-element list holding the monolith, so the single-module world is
        the many-module world with one module.
        """
        loader = self.entry_module().loader
        return [m for m in self.modules if m.loader == loader]

    def bytecode_size(self) -> int:
        """Total bytecode the module table names -- what Bun would run.

        Before 2.1.242 only the entrypoint carried any; the code-split builds
        carry it on every chunk. An edited module names none, so on a patched
        binary this is the untouched modules' total, whatever dead bytes the
        file still holds.
        """
        return sum(m.bytecode[1] for m in self.modules)


def parse(data: bytes) -> Blob:
    """Parse a raw Bun blob (already unwrapped from its container section)."""
    if len(data) < OFFSETS_SIZE + len(TRAILER):
        raise BlobError("blob is too small to hold offsets and trailer")
    if data[-len(TRAILER) :] != TRAILER:
        raise BlobError("missing Bun trailer -- not a Bun standalone payload")

    offsets_at = len(data) - len(TRAILER) - OFFSETS_SIZE
    (byte_count,) = struct.unpack_from("<Q", data, offsets_at)
    table_off, table_len = struct.unpack_from("<II", data, offsets_at + 8)
    (entry_point_id,) = struct.unpack_from("<I", data, offsets_at + 16)
    argv_ptr = struct.unpack_from("<II", data, offsets_at + 20)
    (flags,) = struct.unpack_from("<I", data, offsets_at + 28)

    # Bun writes the count as everything before the struct, and so does
    # :func:`rewrite`; proved here so verify's re-read covers the field too.
    if byte_count != offsets_at:
        raise BlobError(
            f"offsets struct counts {byte_count} bytes before it where "
            f"{offsets_at} lie; not a layout this code knows"
        )
    if table_len % RECORD or table_off + table_len > offsets_at:
        raise BlobError(
            f"module table [{table_off}:{table_off + table_len}] is not a whole "
            f"number of {RECORD}-byte records inside the blob; not a layout "
            "this code knows"
        )
    modules: list[Module] = []
    for index in range(table_len // RECORD):
        base = table_off + index * RECORD
        # Every pair points into the arena before the table; one that runs past
        # it is a corrupt table or a record of another size read as this one,
        # and either way not a blob to write back.
        for at in range(0, RECORD - 4, 8):
            off, length = struct.unpack_from("<II", data, base + at)
            if length and off + length > table_off:
                raise BlobError(
                    f"module {index} names a payload [{off}:{off + length}] past "
                    f"the module table at {table_off}; the record layout does "
                    "not fit"
                )
        modules.append(
            Module(
                index=index,
                contents=struct.unpack_from("<II", data, base + CONTENTS),
                bytecode=struct.unpack_from("<II", data, base + BYTECODE),
                loader=data[base + LOADER],
            )
        )
    if not modules:
        raise BlobError("Bun blob contains no modules")
    if entry_point_id >= len(modules):
        # Bun refuses the same condition when it loads the graph. There is
        # deliberately no fallback to guessing by name: a container that cannot
        # say which module it runs is not one to write to.
        raise BlobError(
            f"entry point id {entry_point_id} is past the end of the "
            f"{len(modules)}-module table"
        )
    table_end = table_off + table_len
    if flags & FLAG_HAS_SOURCE_HASHES and table_end + 4 * len(modules) > offsets_at:
        raise BlobError("the source-hash record runs past the offsets struct")

    return Blob(
        data=data,
        modules=modules,
        table_off=table_off,
        table_len=table_len,
        argv_ptr=argv_ptr,
        entry_point_id=entry_point_id,
        flags=flags,
        offsets_at=offsets_at,
    )


def rewrite(blob: Blob, sources: dict[int, bytes]) -> bytes:
    """Return a blob carrying ``sources`` as those modules' contents, and no
    bytecode for them.

    Nothing pristine moves. The arena is copied whole and the new text appended
    after it (NUL-terminated, as Bun's own ``count_z`` payloads are); the table
    and the tail follow verbatim, shifted by a multiple of :data:`ALIGN` so every
    phase inside them holds. Then the pointers that name what changed are
    re-aimed: each edited module's ``contents`` at its text and ``bytecode`` at
    nothing -- Bun runs a module's bytecode in preference to its source, so a
    stale copy would run the unpatched code -- its source-hash word set to 0, and
    the offsets struct's own pointers moved along with the bytes they name.

    The one statement the write no longer makes is that every module's text lies
    in a single run (``FLAG_SOURCE_TEXT_CONTIGUOUS``, the run Bun's runtime hints
    the kernel to drop after startup): the appended text is outside it. Bun
    documents the absent flag as an older layout it reads, so the flag is cleared
    rather than left asserting something false.
    """
    out = bytearray(blob.data[: blob.table_off])
    placed: dict[int, tuple[int, int]] = {}
    for index, text in sorted(sources.items()):
        placed[index] = (len(out), len(text))
        out += text + b"\0"
    out += b"\0" * ((blob.table_off - len(out)) % ALIGN)
    shift = len(out) - blob.table_off
    out += blob.data[blob.table_off : blob.offsets_at]
    offsets_at = len(out)
    out += blob.data[blob.offsets_at :]

    for index, rng in placed.items():
        base = blob.table_off + shift + index * RECORD
        struct.pack_into("<II", out, base + CONTENTS, *rng)
        struct.pack_into("<II", out, base + BYTECODE, 0, 0)
        if blob.hashes_at is not None:
            struct.pack_into("<I", out, blob.hashes_at + shift + index * 4, 0)

    # A pointer moves with the bytes it names: into the shifted table and tail,
    # or not at all.
    def moved(off: int) -> int:
        return off + shift if off >= blob.table_off else off

    struct.pack_into("<Q", out, offsets_at, offsets_at)
    struct.pack_into("<II", out, offsets_at + 8, moved(blob.table_off), blob.table_len)
    struct.pack_into("<I", out, offsets_at + 16, blob.entry_point_id)
    struct.pack_into(
        "<II", out, offsets_at + 20, moved(blob.argv_ptr[0]), blob.argv_ptr[1]
    )
    struct.pack_into(
        "<I", out, offsets_at + 28, blob.flags & ~FLAG_SOURCE_TEXT_CONTIGUOUS
    )
    return bytes(out)


def changed_modules(blob: Blob, sources: dict[int, bytes]) -> dict[int, bytes]:
    """The subset of ``sources`` whose bytes actually differ from the blob.

    A module the patches parsed but left byte-identical keeps its pointers and
    its bytecode: rewriting it would trade a fast-start module for a recompile
    of code that never changed.
    """
    return {
        index: data
        for index, data in sources.items()
        if data != blob.payload(blob.modules[index].contents)
    }


def unwrap_section(section: bytes) -> bytes:
    """The blob behind the ``u64`` length prefix a container section carries."""
    if len(section) < 8:
        raise BlobError("unrecognised .bun section header")
    (size,) = struct.unpack_from("<Q", section, 0)
    if 8 + size > len(section):
        raise BlobError("the .bun section's size prefix runs past the section")
    return section[8 : 8 + size]


def wrap_section(blob: bytes) -> bytes:
    return struct.pack("<Q", len(blob)) + blob
