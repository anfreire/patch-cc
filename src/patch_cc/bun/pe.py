"""Raw PE32+ surgery for the ``.bun`` section.

Windows turns out to be the *easy* container, for one structural reason: in a
Bun standalone executable ``.bun`` is the last section by both file offset and
virtual address, and the only bytes after it are the Authenticode certificate.
So resizing it moves nothing -- no later section offsets to shift, no segment to
regrow, no relocations to repair. Four header fields restate the new size and
the image ends where the section now ends. The guards here are narrower than the
ELF ones for that reason, and each asserts one half of the "nothing follows"
premise the whole approach rests on.

The certificate is dropped rather than carried over. Editing ``.bun``
invalidates the signature whatever we do, and a stale one is worse than none: it
reads as *tampered* rather than *unsigned*. Nor is there anything to re-sign
with -- Windows runs an unsigned image happily, and a self-signed certificate
would assert an identity that is not ours. ``restore`` puts the signed original
back.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .errors import BunError

DOS_MAGIC = b"MZ"
PE_SIGNATURE = b"PE\0\0"
PE32_PLUS = 0x20B
COFF_SIZE = 20
SECTION_HEADER_SIZE = 40
IMAGE_SCN_CNT_INITIALIZED_DATA = 0x40

#: The largest ``FileAlignment`` the PE specification allows (64 KiB).
_MAX_FILE_ALIGNMENT = 0x10000

#: Data directory index of the Authenticode certificate table. Alone among the
#: directories its "address" is a raw file offset rather than an RVA -- which is
#: also why dropping it is simpler than keeping it.
DIR_CERTIFICATE = 4

# Field offsets inside the PE32+ optional header, for the ones we read or
# restate; ``e_lfanew`` lives at 0x3c in the DOS stub.
_SIZE_OF_INITIALIZED_DATA = 8
_SECTION_ALIGNMENT = 32
_FILE_ALIGNMENT = 36
_SIZE_OF_IMAGE = 56
_CHECKSUM = 64
_NUMBER_OF_RVA_AND_SIZES = 108
_DATA_DIRECTORIES = 112


class PeError(BunError):
    """The binary is not a PE32+ we know how to rewrite safely."""


def _align(value: int, to: int) -> int:
    return -(-value // to) * to


@dataclass(slots=True)
class Section:
    index: int
    name: str
    vsize: int
    vaddr: int
    rawsize: int
    rawptr: int
    characteristics: int

    @property
    def raw_end(self) -> int:
        return self.rawptr + self.rawsize


@dataclass(slots=True)
class Pe:
    opt: int  # file offset of the optional header
    section_table: int  # file offset of the first section header
    symbol_table: int  # COFF symbol table offset; 0 when absent
    sec_align: int
    file_align: int
    directories: list[tuple[int, int]]
    sections: list[Section]

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)

    @property
    def certificate(self) -> tuple[int, int]:
        """The signature's ``(file offset, size)``, or zeroes when unsigned."""
        if len(self.directories) > DIR_CERTIFICATE:
            return self.directories[DIR_CERTIFICATE]
        return (0, 0)


def parse(buf: bytes) -> Pe:
    if len(buf) < 0x40 or buf[:2] != DOS_MAGIC:
        raise PeError("not a PE file")
    (pe_at,) = struct.unpack_from("<I", buf, 0x3C)
    if buf[pe_at : pe_at + len(PE_SIGNATURE)] != PE_SIGNATURE:
        raise PeError("no PE signature -- not a Windows executable")

    # Every read below is bounded first. A PE truncated inside its own headers is
    # the likely shape of a half-finished download, and `struct.error` is not a
    # `BunError` -- it would reach the user as a traceback instead of a sentence.
    coff = pe_at + len(PE_SIGNATURE)
    if len(buf) < coff + COFF_SIZE + 2:
        raise PeError("PE headers are truncated")
    (nsections,) = struct.unpack_from("<H", buf, coff + 2)
    (symbol_table,) = struct.unpack_from("<I", buf, coff + 8)
    (opt_size,) = struct.unpack_from("<H", buf, coff + 16)
    opt = coff + COFF_SIZE

    (magic,) = struct.unpack_from("<H", buf, opt)
    if magic != PE32_PLUS:
        # Both Windows targets Claude ships are 64-bit and the official installer
        # refuses 32-bit outright, so a PE32 field layout is one we would carry
        # without ever running.
        raise PeError("only PE32+ (64-bit) executables are supported")
    if opt_size < _DATA_DIRECTORIES or len(buf) < opt + opt_size:
        raise PeError("PE optional header is truncated")

    sec_align, file_align = struct.unpack_from("<II", buf, opt + _SECTION_ALIGNMENT)
    if not sec_align or not file_align:
        raise PeError("PE declares a zero alignment")
    if file_align > _MAX_FILE_ALIGNMENT or file_align & (file_align - 1):
        # The spec's own range. Unbounded, a header claiming 0x40000000 would pad
        # `.bun` out to a gigabyte of zeroes -- and every check downstream would
        # pass, because the image really would be the size its header claims.
        raise PeError(
            f"PE declares a FileAlignment of {file_align:#x}, which is not a "
            f"power of two within {_MAX_FILE_ALIGNMENT:#x}"
        )

    # `NumberOfRvaAndSizes` is clamped to what the optional header can hold, so a
    # header claiming more directories than it carries cannot make us read
    # section-table bytes as directory entries.
    (ndirs,) = struct.unpack_from("<I", buf, opt + _NUMBER_OF_RVA_AND_SIZES)
    ndirs = min(ndirs, (opt_size - _DATA_DIRECTORIES) // 8)
    directories = [
        struct.unpack_from("<II", buf, opt + _DATA_DIRECTORIES + i * 8)
        for i in range(ndirs)
    ]

    section_table = opt + opt_size
    if len(buf) < section_table + nsections * SECTION_HEADER_SIZE:
        raise PeError("PE section table is truncated")
    sections = []
    for index in range(nsections):
        base = section_table + index * SECTION_HEADER_SIZE
        name = buf[base : base + 8].rstrip(b"\0").decode("utf8", "replace")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", buf, base + 8)
        (characteristics,) = struct.unpack_from("<I", buf, base + 36)
        sections.append(
            Section(index, name, vsize, vaddr, rawsize, rawptr, characteristics)
        )
    if not sections:
        raise PeError("PE has no sections")

    return Pe(
        opt=opt,
        section_table=section_table,
        symbol_table=symbol_table,
        sec_align=sec_align,
        file_align=file_align,
        directories=directories,
        sections=sections,
    )


def read_section(buf: bytes, name: str = ".bun") -> bytes:
    pe = parse(buf)
    section = pe.section(name)
    if section is None:
        raise PeError(f"no {name} section in this PE")
    return buf[section.rawptr : section.raw_end]


def _check_resize_is_safe(pe: Pe, bun: Section, size: int) -> None:
    """Refuse anything that contradicts "``.bun`` is last".

    Every test is on an *extent*, not a start: a section beginning before ``.bun``
    and ending after it owns bytes we are about to overwrite, and one mapped
    across ``.bun``'s address would fall outside the image once ``SizeOfImage``
    shrinks -- both of which a start-only comparison waves through.
    """
    if bun.raw_end > size:
        # Before the tail arithmetic below, which would otherwise report a
        # negative count and call a short file a long one.
        raise PeError(
            f".bun claims {bun.rawsize:,} bytes at offset {bun.rawptr:,} but the "
            f"file holds only {size:,}; it is truncated"
        )

    others = [s for s in pe.sections if s.index != bun.index]

    later = [s for s in others if s.rawsize and s.raw_end > bun.rawptr]
    if later:
        names = ", ".join(s.name or f"<{s.index}>" for s in later)
        raise PeError(f"cannot resize .bun with section payloads after it: {names}")

    above = [s for s in others if s.vaddr + s.vsize > bun.vaddr]
    if above:
        names = ", ".join(s.name or f"<{s.index}>" for s in above)
        raise PeError(f"cannot resize .bun with sections mapped above it: {names}")

    if pe.symbol_table and pe.symbol_table >= bun.rawptr:
        raise PeError("cannot resize .bun with the COFF symbol table after it")

    # A directory reaching into .bun would mean the loader reads structure from
    # the bytes we replace wholesale. The certificate is excluded because its
    # address is a file offset, not an RVA, and because it is the one we drop.
    inside = [
        i
        for i, (rva, dsize) in enumerate(pe.directories)
        if i != DIR_CERTIFICATE and dsize and rva + dsize > bun.vaddr
    ]
    if inside:
        raise PeError(f"data directories are mapped inside .bun: {inside}")

    cert_at, cert_size = pe.certificate
    signed = bool(cert_size) and cert_at >= bun.raw_end and cert_at + cert_size == size
    if not signed and size != bun.raw_end:
        raise PeError(
            f"{size - bun.raw_end:,} bytes follow .bun that are not a signature; "
            "refusing to truncate data we cannot account for"
        )


def write_section(buf: bytes, payload: bytes, name: str = ".bun") -> bytes:
    """Return a new PE image with ``name``'s contents replaced by ``payload``.

    The section keeps its file offset and its virtual address; only its two
    sizes, the image size and the initialized-data total change. The signature
    goes, and the file ends where the section does.
    """
    pe = parse(buf)
    bun = pe.section(name)
    if bun is None:
        raise PeError(f"no {name} section in this PE")
    _check_resize_is_safe(pe, bun, len(buf))

    # VirtualSize is the payload exactly; SizeOfRawData is it padded, because PE
    # requires a multiple of FileAlignment. That mirrors what the linker emitted.
    vsize = len(payload)
    rawsize = _align(vsize, pe.file_align)
    out = bytearray(buf[: bun.rawptr])
    out += payload
    out += b"\0" * (rawsize - vsize)

    header = pe.section_table + bun.index * SECTION_HEADER_SIZE
    struct.pack_into("<I", out, header + 8, vsize)
    struct.pack_into("<I", out, header + 16, rawsize)
    struct.pack_into(
        "<I", out, pe.opt + _SIZE_OF_IMAGE, _align(bun.vaddr + vsize, pe.sec_align)
    )

    if bun.characteristics & IMAGE_SCN_CNT_INITIALIZED_DATA:
        # The linker counts every initialized section's raw size here, .bun
        # included. Nothing loads from the figure, but leaving the old one would
        # have the header describe a file that no longer exists.
        at = pe.opt + _SIZE_OF_INITIALIZED_DATA
        (declared,) = struct.unpack_from("<I", out, at)
        if declared < bun.rawsize:
            # A total cannot be smaller than one of its terms. Clamping would swap
            # one figure that misdescribes the file for another.
            raise PeError(
                f"SizeOfInitializedData ({declared:,}) is smaller than .bun's own "
                f"raw size ({bun.rawsize:,}); this header does not add up"
            )
        struct.pack_into("<I", out, at, declared - bun.rawsize + rawsize)

    if pe.certificate != (0, 0):
        # The bytes are already gone -- they lived past .bun's raw end, which is
        # where the image now stops. Only the pointer is left to clear.
        struct.pack_into(
            "<II", out, pe.opt + _DATA_DIRECTORIES + DIR_CERTIFICATE * 8, 0, 0
        )

    _set_checksum(pe, out)
    return bytes(out)


def _set_checksum(pe: Pe, out: bytearray) -> None:
    """Restate ``CheckSum`` over the finished image.

    Windows does not verify it for user-mode executables, so this is not
    load-bearing. It is here for the same reason the dead signature is not: a
    header field describing bytes the file no longer contains is a lie, and the
    ones'-complement sum that fixes it is six lines. Summed through a cast view,
    which copies nothing -- and so reads native order, which is the order of
    every host that runs a PE.
    """
    at = pe.opt + _CHECKSUM
    struct.pack_into("<I", out, at, 0)

    even = len(out) - len(out) % 2
    total = sum(memoryview(out)[:even].cast("H"))
    if even != len(out):
        total += out[-1]  # unreachable for a real FileAlignment, which pads even
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    struct.pack_into("<I", out, at, total + len(out))
