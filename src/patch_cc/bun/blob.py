"""Read and rewrite the Bun standalone-executable blob.

Layout of the blob (little-endian throughout)::

    [ data arena: name / contents / sourcemap / bytecode / ... payloads ]
    [ module table: N records of `struct_size` bytes                    ]
    [ compileExecArgv payload                                           ]
    [ 32-byte offsets struct                                            ]
    [ 15-byte "\\n---- Bun! ----\\n" trailer                              ]

Every pointer in the blob is a ``(u32 offset, u32 length)`` pair relative to the
start of the blob, and they live in exactly two places: the module table and the
offsets struct. That is what makes rewriting tractable -- move a payload, then
fix up the pointers that describe it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import BunError

TRAILER = b"\n---- Bun! ----\n"
OFFSETS_SIZE = 32

#: Module record field order. Old Bun (<1.3.7) stops after ``bytecode``.
FIELDS_NEW = (
    "name",
    "contents",
    "sourcemap",
    "bytecode",
    "module_info",
    "bytecode_origin_path",
)
FIELDS_OLD = FIELDS_NEW[:4]

#: Names Bun gives the Claude entrypoint module across platforms/versions.
ENTRY_NAMES = ("claude", "claude.exe", "src/entrypoints/cli.js")


class BlobError(BunError):
    """The .bun payload did not look like a Bun module graph."""


def is_entry_module(name: str) -> bool:
    return any(name == n or name.endswith("/" + n) for n in ENTRY_NAMES)


@dataclass(slots=True)
class Module:
    index: int
    ranges: dict[str, tuple[int, int]]
    trailing: bytes  # encoding, loader, module_format, side

    @property
    def name_range(self) -> tuple[int, int]:
        return self.ranges["name"]


@dataclass(slots=True)
class Blob:
    data: bytes
    struct_size: int
    modules: list[Module]
    modules_ptr: tuple[int, int]
    argv_ptr: tuple[int, int]
    entry_point_id: int
    flags: int
    offsets_at: int

    @property
    def fields(self) -> tuple[str, ...]:
        return FIELDS_NEW if self.struct_size == 52 else FIELDS_OLD

    def payload(self, rng: tuple[int, int]) -> bytes:
        off, length = rng
        return self.data[off : off + length]

    def module_name(self, module: Module) -> str:
        return self.payload(module.name_range).decode("utf8", "replace")

    def entry_module(self) -> Module:
        for module in self.modules:
            if is_entry_module(self.module_name(module)):
                return module
        raise BlobError("no Claude entrypoint module in the Bun blob")

    def entry_source(self) -> bytes:
        return self.payload(self.entry_module().ranges["contents"])

    def bytecode_size(self) -> int:
        return (
            self.entry_module().ranges["bytecode"][1] if self.struct_size == 52 else 0
        )


def _detect_struct_size(modules_len: int) -> int:
    new_ok = modules_len % 52 == 0
    old_ok = modules_len % 36 == 0
    if new_ok and not old_ok:
        return 52
    if old_ok and not new_ok:
        return 36
    return 52


def parse(data: bytes) -> Blob:
    """Parse a raw Bun blob (already unwrapped from its container section)."""
    if len(data) < OFFSETS_SIZE + len(TRAILER):
        raise BlobError("blob is too small to hold offsets and trailer")
    if data[-len(TRAILER) :] != TRAILER:
        raise BlobError("missing Bun trailer -- not a Bun standalone payload")

    offsets_at = len(data) - len(TRAILER) - OFFSETS_SIZE
    (_byte_count,) = struct.unpack_from("<Q", data, offsets_at)
    modules_ptr = struct.unpack_from("<II", data, offsets_at + 8)
    (entry_point_id,) = struct.unpack_from("<I", data, offsets_at + 16)
    argv_ptr = struct.unpack_from("<II", data, offsets_at + 20)
    (flags,) = struct.unpack_from("<I", data, offsets_at + 28)

    struct_size = _detect_struct_size(modules_ptr[1])
    fields = FIELDS_NEW if struct_size == 52 else FIELDS_OLD
    table_off, table_len = modules_ptr
    if table_off + table_len > len(data):
        raise BlobError("module table runs past the end of the blob")

    modules: list[Module] = []
    for index in range(table_len // struct_size):
        base = table_off + index * struct_size
        ranges = {
            field: struct.unpack_from("<II", data, base + i * 8)
            for i, field in enumerate(fields)
        }
        trailing = data[base + len(fields) * 8 : base + len(fields) * 8 + 4]
        modules.append(Module(index=index, ranges=ranges, trailing=trailing))

    if not modules:
        raise BlobError("Bun blob contains no modules")

    return Blob(
        data=data,
        struct_size=struct_size,
        modules=modules,
        modules_ptr=modules_ptr,
        argv_ptr=argv_ptr,
        entry_point_id=entry_point_id,
        flags=flags,
        offsets_at=offsets_at,
    )


def rebuild(blob: Blob, source: bytes, *, drop_bytecode: bool = True) -> bytes:
    """Return a new blob with the entrypoint's source replaced.

    Payloads are re-emitted in their original file order so the result stays as
    close to the input layout as possible.

    ``drop_bytecode`` removes the entrypoint's precompiled Bun bytecode. Editing
    the source invalidates that bytecode anyway -- Bun recompiles from source --
    so keeping it costs ~154 MB for no benefit. See docs/INTERNALS.md.
    """
    entry = blob.entry_module()
    fields = blob.fields
    has_bytecode = "bytecode" in fields

    # Every payload, in the order it appears in the source arena.
    placed: list[
        tuple[int, int, int, str]
    ] = []  # (offset, length, module_index, field)
    for module in blob.modules:
        for field in fields:
            off, length = module.ranges[field]
            if length:
                placed.append((off, length, module.index, field))
    placed.sort()

    out = bytearray()
    new_ranges: dict[tuple[int, str], tuple[int, int]] = {}
    prev_end = 0
    for off, length, mod_index, field in placed:
        is_entry = mod_index == entry.index
        if is_entry and field == "bytecode" and drop_bytecode:
            new_ranges[(mod_index, field)] = (0, 0)
            prev_end = off + length
            continue

        payload = (
            source
            if (is_entry and field == "contents")
            else blob.data[off : off + length]
        )
        # Preserve the 1-byte separators Bun emits between payloads.
        if prev_end and off > prev_end:
            out += b"\0" * (off - prev_end)
        new_ranges[(mod_index, field)] = (len(out), len(payload))
        out += payload
        prev_end = off + length

    out += b"\0"
    table_off = len(out)
    table_len = len(blob.modules) * blob.struct_size
    out += bytearray(table_len)

    argv = blob.payload(blob.argv_ptr)
    argv_off = len(out)
    out += argv + b"\0"

    offsets_at = len(out)
    out += bytearray(OFFSETS_SIZE)
    out += TRAILER

    for module in blob.modules:
        base = table_off + module.index * blob.struct_size
        for i, field in enumerate(fields):
            off, length = new_ranges.get((module.index, field), (0, 0))
            struct.pack_into("<II", out, base + i * 8, off, length)
        tail = base + len(fields) * 8
        out[tail : tail + 4] = module.trailing

    struct.pack_into("<Q", out, offsets_at, offsets_at)
    struct.pack_into("<II", out, offsets_at + 8, table_off, table_len)
    struct.pack_into("<I", out, offsets_at + 16, blob.entry_point_id)
    struct.pack_into("<II", out, offsets_at + 20, argv_off, len(argv))
    struct.pack_into("<I", out, offsets_at + 28, blob.flags)

    if has_bytecode and drop_bytecode:
        assert new_ranges[(entry.index, "bytecode")] == (0, 0)
    return bytes(out)


def unwrap_section(section: bytes) -> tuple[bytes, int]:
    """Strip the length prefix a container section puts in front of the blob.

    Bun >=1.3.4 uses a u64 prefix; older builds use u32. Both are followed by
    padding up to the section's alignment, hence the 4 KiB slack window.
    """
    size = len(section)
    if size >= 8:
        (as64,) = struct.unpack_from("<Q", section, 0)
        if 8 + as64 <= size and 8 + as64 >= size - 4096:
            return section[8 : 8 + as64], 8
    if size >= 4:
        (as32,) = struct.unpack_from("<I", section, 0)
        if 4 + as32 <= size and 4 + as32 >= size - 4096:
            return section[4 : 4 + as32], 4
    raise BlobError("unrecognised .bun section header")


def wrap_section(blob: bytes, header_size: int) -> bytes:
    prefix = (
        struct.pack("<Q", len(blob))
        if header_size == 8
        else struct.pack("<I", len(blob))
    )
    return prefix + blob
