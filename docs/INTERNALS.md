# Internals

How patch-cc gets from a 267 MB binary to a patched, smaller one.

## The shape of a native Claude binary

Claude Code ships as a [Bun](https://bun.sh) single-file executable. The whole
app — a ~20 MB minified JS bundle plus a few asset modules — is embedded in the
binary:

- **Linux**: an ELF section named `.bun`
- **macOS**: a Mach-O section `__BUN,__bun`
- **Windows**: a PE `.bun` section

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

### Windows: the easy one

PE (`pe.py`) needs no shifting at all. In a Bun executable `.bun` is the **last**
section by both file offset and virtual address, and the only bytes past it are
the Authenticode certificate — so resizing it moves nothing. Four header fields
restate the size and the image ends where the section now ends:

| field | new value |
|---|---|
| `.bun` `VirtualSize` | the payload length exactly |
| `.bun` `SizeOfRawData` | that, padded up to `FileAlignment` |
| `SizeOfImage` | `align(.bun.VirtualAddress + VirtualSize, SectionAlignment)` |
| `SizeOfInitializedData` | adjusted by the change in raw size |

The guards are narrower than the ELF ones because the claim is narrower, but
each asserts one half of "nothing follows": no later section payload, nothing
mapped above, no COFF symbol table behind us, no data directory reaching into
`.bun`, and a file tail that is the signature rather than something we would
truncate away unexamined.

**The signature is dropped, not updated.** Editing `.bun` invalidates it
whatever we do, and a stale signature is worse than none — it reads as
*tampered* rather than *unsigned*. Nothing is re-signed: macOS signs ad-hoc only
because arm64 refuses to run an unsigned image, Windows runs one happily, and a
self-signed certificate would assert an identity that is not ours. `restore`
copies the pristine backup back, signature and all. `CheckSum` is recomputed for
the same honesty — Windows does not verify it for user-mode executables, so
check it against `imagehlp!MapFileAndCheckSumW`, which is the authority, not our
own arithmetic.

No third-party dependency: PE is pure `struct`, and LIEF stays macOS-only.

### Replacing the file

`container.replace` puts the staged binary in place. POSIX swaps a directory
entry and any running process keeps the inode it already mapped. Windows refuses
to *overwrite* an image mapped for execution but does allow *renaming* one, so a
running `claude.exe` is parked at `claude.exe.patch-cc.old` and the new binary
takes its name — which is what keeps "restart Claude Code for changes to take
effect" the answer on either platform rather than "close it first".

A parked image cannot be deleted until its process exits, so the sweep is the
first thing a *later* replace does, not the last thing this one does — down
there it could never fire, since the only path reaching it runs while the file is
still held. The name takes a counter, because more than one generation can be
mapped at once: patch, start a fresh session as instructed, leave the previous
one open, patch again. Once every slot is taken the patch fails saying so, rather
than hunting indefinitely.

If the new binary cannot take the name after the old one has been parked, the old
one is moved back before the error propagates. If even *that* fails, `claude.exe`
does not exist — so the error says where the working binary is parked and what to
rename it to; nothing else in the tree would tell the user.

## The manifest

Every patched bundle ends with a single comment line — the one description of
its shape; [PLAYBOOK.md](PLAYBOOK.md) covers what it means for matcher health:

```
//patch-cc {"v":1,"tool":"<version>","patches":[...],"brand":...,"models":{...},
            "org":...,"codex":{"port":8817,"models":["gpt-5.6-sol", ...]}}
```

That line is why `patch-cc status` can name exactly what is applied: several
patches are value flips (`verbose:!0`) that leave no other trace. A comment
can't collide with code and travels with the bundle through extract/repack.
The menu also reads it to pre-select the current patch set — the binary is the
state.

Every key after `patches` belongs to a patch and is written only when *that*
patch landed, so `status` can never assert a name, marker, or model the bundle
does not contain.

Each key records what was *asked for*, never what was derived from it. `codex`
carries model ids and a port and nothing else: a Codex model's display name and
context window are already baked into the bundle, and repeating them here would
be a second copy — one that a relabelling upstream could make disagree with the
binary it claims to describe. That is also what makes the manifest the single
home for the gateway port: `codex serve` and `codex status` read it from here
rather than from a store of their own.

## Safety

- Before the first patch of a version, the pristine binary is copied to
  `~/.local/share/patch-cc/backups/`. `restore` copies it back — never an
  inverse patch (insertions cascade, so a reverse diff is meaningless).
- Patching always starts from a pristine source, so re-applying never stacks
  edits on edits, and an apply where **no** patch changes anything leaves the
  binary untouched entirely (stripping bytecode for nothing would only slow
  startup).
- *Which* pristine source: the installed binary while it is unpatched, and the
  kept copy only once it is not. An update replaces the whole binary, so
  "installed and unpatched" means "installed and pristine" — the original of the
  version installed now, which a backup only is for the version it was taken
  from. Those coincide wherever the launcher is version-named and part company
  wherever it is a fixed path (every Windows install, and any Homebrew or npm
  one), where preferring the backup would patch a superseded bundle over the
  current install. An install we cannot *read* is not a pristine source of
  anything and falls through to the kept copy — on Windows the likely shape is a
  sharing violation from a scanner or an in-flight updater, which never reaches
  the parser at all.
- The one branch that can overwrite a good backup also asks whether the bundle
  still carries entrypoint bytecode. Everything else here rests on the manifest
  fingerprint, which has changed once already; bytecode cannot mislead the same
  way, because every shipped build has ~154 MB of it and `verify` refuses to emit
  a binary that has any.
- `restore` reads the **backup** before installing it — the check belongs on the
  file being written, since a copy truncated by a full disk would otherwise land
  over a working binary and be reported as a success. It declines only when the
  install is readable, unpatched *and* unversioned, where the kept copy is of an
  older release; it never reads the install to decide whether it *may* proceed,
  since making un-bricking conditional on the brick being readable would invert
  the point of the command.
- Every patch write is verified: patch-cc re-extracts the JS from the binary it
  just wrote and asserts it equals what it meant to write.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused outright — there is nothing clean to start from, and our edits change
  lengths, so a second pass would corrupt rather than update. `restore` or a
  reinstall are the only honest fixes; there is deliberately no override.
