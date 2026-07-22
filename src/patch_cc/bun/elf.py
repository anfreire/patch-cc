"""Raw ELF64 surgery for the ``.bun`` section.

We deliberately do not use a generic ELF library to *write*. LIEF rebuilds the
binary and relocates ``.bun`` so its file offset matches its virtual address
(0x20000000), which inflates a 267 MB binary to 715 MB. Editing the bytes in
place keeps ``.bun`` where it is and grows the file by only the delta.
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass

from .errors import BunError

SHT_NOBITS = 8
SHF_ALLOC = 0x2
PT_LOAD = 1


class ElfError(BunError):
    """The binary is not an ELF64 we know how to rewrite safely."""


@dataclass(slots=True)
class Section:
    index: int
    name: str
    type: int
    flags: int
    addr: int
    offset: int
    size: int
    align: int

    @property
    def has_file_payload(self) -> bool:
        return self.type != SHT_NOBITS and self.size > 0

    @property
    def is_alloc(self) -> bool:
        return bool(self.flags & SHF_ALLOC)


@dataclass(slots=True)
class Segment:
    index: int
    type: int
    offset: int
    vaddr: int
    filesz: int
    memsz: int
    align: int


@dataclass(slots=True)
class Elf:
    phoff: int
    shoff: int
    phentsize: int
    phnum: int
    shentsize: int
    shnum: int
    sections: list[Section]
    segments: list[Segment]

    def section(self, name: str) -> Section | None:
        return next((s for s in self.sections if s.name == name), None)


def parse(buf: bytes) -> Elf:
    if len(buf) < 64 or buf[:4] != b"\x7fELF":
        raise ElfError("not an ELF file")
    if buf[4] != 2:
        raise ElfError("only ELF64 is supported")
    if buf[5] != 1:
        raise ElfError("only little-endian ELF is supported")

    phoff, shoff = struct.unpack_from("<QQ", buf, 32)
    phentsize, phnum, shentsize, shnum, shstrndx = struct.unpack_from("<HHHHH", buf, 54)
    if not shnum or shstrndx >= shnum:
        raise ElfError("ELF has no usable section header table")

    strbase = shoff + shstrndx * shentsize
    stroff, strsize = struct.unpack_from("<QQ", buf, strbase + 24)
    strtab = buf[stroff : stroff + strsize]

    sections: list[Section] = []
    for index in range(shnum):
        base = shoff + index * shentsize
        name_off, sh_type = struct.unpack_from("<II", buf, base)
        flags, addr, offset, size = struct.unpack_from("<QQQQ", buf, base + 8)
        (align,) = struct.unpack_from("<Q", buf, base + 48)
        end = strtab.find(b"\0", name_off)
        name = strtab[name_off : end if end != -1 else None].decode("utf8", "replace")
        sections.append(Section(index, name, sh_type, flags, addr, offset, size, align))

    segments: list[Segment] = []
    for index in range(phnum):
        base = phoff + index * phentsize
        (p_type,) = struct.unpack_from("<I", buf, base)
        offset, vaddr, _paddr, filesz, memsz = struct.unpack_from(
            "<QQQQQ", buf, base + 8
        )
        (align,) = struct.unpack_from("<Q", buf, base + 48)
        segments.append(Segment(index, p_type, offset, vaddr, filesz, memsz, align))

    return Elf(phoff, shoff, phentsize, phnum, shentsize, shnum, sections, segments)


def read_section(buf: bytes, name: str = ".bun") -> bytes:
    elf = parse(buf)
    section = elf.section(name)
    if section is None:
        raise ElfError(f"no {name} section in this ELF")
    return buf[section.offset : section.offset + section.size]


def _containing_load(elf: Elf, section: Section) -> Segment | None:
    file_end = section.offset + section.size
    virt_end = section.addr + section.size
    for seg in elf.segments:
        if seg.type != PT_LOAD:
            continue
        if (
            seg.offset <= section.offset
            and file_end <= seg.offset + seg.filesz
            and seg.vaddr <= section.addr
            and virt_end <= seg.vaddr + seg.memsz
        ):
            return seg
    return None


def _check_growth_is_safe(elf: Elf, bun: Section, shifted: list[Section]) -> None:
    """Refuse anything that could change runtime mapping semantics."""
    end = bun.offset + bun.size

    overlapping = [
        s
        for s in elf.sections
        if s.index != bun.index and s.has_file_payload and bun.offset < s.offset < end
    ]
    if overlapping:
        names = ", ".join(s.name or f"<{s.index}>" for s in overlapping)
        raise ElfError(f".bun overlaps later section payloads: {names}")

    alloc = [s for s in shifted if s.is_alloc]
    if alloc:
        names = ", ".join(s.name or f"<{s.index}>" for s in alloc)
        raise ElfError(f"cannot move allocated sections that follow .bun: {names}")

    sh_end = elf.shoff + elf.shentsize * elf.shnum
    if elf.shoff < end < sh_end:
        raise ElfError("cannot resize .bun from inside the section header table")
    ph_end = elf.phoff + elf.phentsize * elf.phnum
    if elf.phoff < end < ph_end:
        raise ElfError("cannot resize .bun from inside the program header table")

    container = _containing_load(elf, bun)
    spanning = [
        seg
        for seg in elf.segments
        if seg.index != (container.index if container else -1)
        and seg.filesz
        and seg.offset < end < seg.offset + seg.filesz
    ]
    if spanning:
        names = ", ".join(f"{s.index}:{s.type}" for s in spanning)
        raise ElfError(f"cannot resize .bun inside unrelated segments: {names}")


def _alignment_step(elf: Elf, end: int, shifted: list[Section]) -> int:
    """Smallest shift that preserves the alignment of everything we move."""
    step = 1
    for section in shifted:
        step = max(step, section.align or 1)
    for seg in elf.segments:
        if seg.filesz and seg.offset >= end:
            step = max(step, seg.align or 1)
    return step


def write_section(buf: bytes, payload: bytes, name: str = ".bun") -> bytes:
    """Return a new ELF image with ``name``'s contents replaced by ``payload``.

    The section keeps its file offset. Everything after it shifts by a delta
    rounded to a multiple of the strictest alignment among the moved pieces, and
    the containing ``PT_LOAD`` grows or shrinks to match.
    """
    elf = parse(buf)
    bun = elf.section(name)
    if bun is None:
        raise ElfError(f"no {name} section in this ELF")

    start, end = bun.offset, bun.offset + bun.size
    shifted = [
        s
        for s in elf.sections
        if s.index != bun.index and s.has_file_payload and s.offset >= end
    ]

    delta = len(payload) - bun.size
    if delta:
        _check_growth_is_safe(elf, bun, shifted)
        step = _alignment_step(elf, end, shifted)
        if step > 1:
            # Grow generously / shrink conservatively so offsets stay congruent.
            delta = (
                (delta + step - 1) // step * step
                if delta > 0
                else -((-delta) // step * step)
            )

    container = _containing_load(elf, bun)
    if delta and container is None:
        raise ElfError(".bun has no containing PT_LOAD segment")

    for seg in elf.segments:
        if (
            delta
            and seg.type == PT_LOAD
            and seg.filesz
            and seg.offset >= end
            and seg.align
        ):
            if (seg.offset + delta) % seg.align != seg.vaddr % seg.align:
                raise ElfError(
                    f"shifting LOAD segment {seg.index} would break its alignment"
                )

    new_size = bun.size + delta
    body = bytearray(payload)
    if len(body) < new_size:
        body += b"\0" * (new_size - len(body))
    elif len(body) > new_size:
        raise ElfError("internal error: payload larger than the resized section")

    out = bytearray(buf[:start]) + body + bytearray(buf[end:])

    new_phoff = elf.phoff + delta if elf.phoff >= end else elf.phoff
    new_shoff = elf.shoff + delta if elf.shoff >= end else elf.shoff
    struct.pack_into("<QQ", out, 32, new_phoff, new_shoff)

    for section in elf.sections:
        base = new_shoff + section.index * elf.shentsize
        if section.index == bun.index:
            struct.pack_into("<Q", out, base + 32, new_size)
        elif delta and section.has_file_payload and section.offset >= end:
            struct.pack_into("<Q", out, base + 24, section.offset + delta)

    for seg in elf.segments:
        base = new_phoff + seg.index * elf.phentsize
        if delta and container is not None and seg.index == container.index:
            struct.pack_into(
                "<QQ", out, base + 32, seg.filesz + delta, seg.memsz + delta
            )
        elif delta and seg.filesz and seg.offset >= end:
            struct.pack_into("<Q", out, base + 8, seg.offset + delta)

    return bytes(out)


def atomic_write(path: str, data: bytes, mode_from: str | None = None) -> None:
    """Write ``data`` to ``path`` via a temp file, preserving the mode bits."""
    tmp = f"{path}.patch-cc.tmp"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
        source = mode_from or (path if os.path.exists(path) else None)
        if source:
            os.chmod(tmp, os.stat(source).st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        raise
