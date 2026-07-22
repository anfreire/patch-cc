# Internals

How patch-cc gets from a 267 MB binary to a patched, smaller one.

## The shape of a native Claude binary

Claude Code ships as a [Bun](https://bun.sh) single-file executable. The whole
app — a ~20 MB minified JS bundle plus a few asset modules — is embedded in the
binary:

- **Linux**: an ELF section named `.bun`
- **macOS**: a Mach-O section `__BUN,__bun`
- **Windows**: a PE `.bun` section (not supported here)

Inside that section is a *Bun module graph*: a flat arena of payloads, a module
table describing them, and a trailer.

```
.bun section
└── [u64 size prefix]           (u32 on Bun < 1.3.4)
    └── Bun blob
        ├── payload arena       name / contents / sourcemap / bytecode / ... bytes
        ├── module table        N records × 52 bytes (36 on old Bun)
        ├── compileExecArgv
        ├── offsets struct       32 bytes: byteCount, modulesPtr, entryId, argvPtr, flags
        └── "\n---- Bun! ----\n"  15-byte trailer
```

Every pointer is a `(u32 offset, u32 length)` pair relative to the blob start,
and pointers live in only two places: the module table and the offsets struct.
That is what makes rewriting tractable — move a payload, fix the handful of
pointers that describe it.

A module record (new 52-byte format) is six such pairs — `name`, `contents`,
`sourcemap`, `bytecode`, `moduleInfo`, `bytecodeOriginPath` — followed by four
`u8` flags (`encoding`, `loader`, `moduleFormat`, `side`).

The module we patch is the entrypoint, named `/$bunfs/root/src/entrypoints/cli.js`
(or `claude` / `claude.exe` on other builds). Its `contents` is the JS we edit.

Code: `src/patch_cc/bun/blob.py`.

## The 154 MB bytecode, and why we drop it

The entry module also carries ~154 MB of precompiled Bun **bytecode** — more
than half the binary. Every other module has none.

Any edit to `contents` invalidates that bytecode; Bun detects the mismatch and
recompiles from source at launch. So keeping it buys nothing:

| binary | size | startup |
|---|---|---|
| original (valid bytecode) | 267 MB | ~100 ms |
| patched, bytecode kept | 267 MB | ~650 ms |
| patched, bytecode dropped | **113 MB** | ~650 ms |

Patching pays the recompile cost either way, so patch-cc drops the entry
module's bytecode (`rebuild(..., drop_bytecode=True)`). The result runs source,
guaranteeing our edits are authoritative, and is ~154 MB smaller.

`doctor` asserts the patched binary has `bytecode == 0`. If a future Bun build
makes bytecode authoritative over source, that assert is the tripwire — every
patch would silently no-op otherwise.

## Writing it back without ballooning

`.bun` is the last *allocated* ELF section; only non-allocated metadata
(`.comment`, `.symtab`, `.strtab`, `.shstrtab`) follows it. patch-cc rewrites
the ELF bytes in place:

1. Splice the new (smaller) blob over the old `.bun` bytes.
2. Shift `e_shoff`, `e_phoff`, and the trailing non-alloc sections/segments by
   the size delta.
3. Grow or shrink the containing `PT_LOAD` segment's `filesz`/`memsz` to match.

`.bun` keeps its original file offset. This is deliberately *not* done with a
general ELF library: LIEF rebuilds the binary and relocates `.bun` so its file
offset equals its virtual address (`0x20000000`), which inflates the file to
~715 MB. Raw in-place surgery avoids that entirely.

Guards refuse anything that could corrupt the mapping: allocated sections after
`.bun`, growth into a header table, an unrelated spanning segment, or a
misaligned `PT_LOAD` shift. If any fires, the write aborts rather than guesses.

Code: `src/patch_cc/bun/elf.py`. macOS uses LIEF (`macho.py`) — Mach-O segment
growth is page-aligned and bounded, with no relocation pathology, and every
edit is followed by an ad-hoc `codesign` (mandatory on Apple Silicon).

## The manifest

Every patched bundle ends with a single comment line:

```
//patch-cc {"v":1,"tool":"0.1.0","patches":[...],"brand":...,"models":{...}}
```

That line is why `patch-cc status` can name exactly what is applied: several
patches are value flips (`verbose:!0`) that leave no other trace. A comment
can't collide with code and travels with the bundle through extract/repack.
The menu also reads it to pre-select the current patch set — the binary is the
state.

## Safety

- Before the first patch of a version, the pristine binary is copied to
  `~/.local/share/patch-cc/backups/`. `restore` copies it back — never an
  inverse patch (insertions cascade, so a reverse diff is meaningless).
- Patching always starts from that pristine copy, so re-applying never stacks
  edits on edits, and an apply where **no** patch changes anything leaves the
  binary untouched entirely (stripping bytecode for nothing would only slow
  startup).
- Every write is verified: patch-cc re-extracts the JS from the binary it just
  wrote and asserts it equals what it meant to write.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused unless `--force` — there is nothing clean to start from.
