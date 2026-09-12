# Internals

How patch-cc gets from a Claude binary to a patched one.

## The shape of a native Claude binary

Claude Code ships as a [Bun](https://bun.sh) single-file executable. The whole
app — 34 MB of minified JS on 2.1.269, plus a few asset modules — is embedded
in the binary:

- **Linux**: an ELF section named `.bun`
- **macOS**: a Mach-O section `__BUN,__bun`
- **Windows**: a PE `.bun` section (not supported here)

Inside that section is a *Bun module graph*: a flat arena of payloads, a module
table describing them, and a trailer. Before 2.1.242 the graph was the app in one
module plus a few asset modules; since then the app is **code-split** across
~1,300 `chunk-*.js` modules that the entry lazily imports (see
[the split](#the-21242-split-and-the-patchable-surface)). Either way patch-cc
treats every module the container declares to be JavaScript as one surface.

```
.bun section
└── [u64 size prefix]
    └── Bun blob
        ├── payload arena       name / contents / sourcemap / bytecode / ... bytes,
        │                       and whatever else the builder placed there
        ├── module table        N records × 52 bytes
        ├── records             flag-gated, see below
        ├── compileExecArgv
        ├── offsets struct       32 bytes: byteCount, modulesPtr, entryId, argvPtr, flags
        └── "\n---- Bun! ----\n"  15-byte trailer
```

Every pointer is a `(u32 offset, u32 length)` pair relative to the blob start,
and Bun reads a payload *through* its pointer — bytes nothing points at are
ignored. A module record is six such pairs — `name`, `contents`, `sourcemap`,
`bytecode`, `moduleInfo`, `bytecodeOriginPath` — followed by four `u8` flags
(`encoding`, `loader`, `moduleFormat`, `side`).

Code: `src/patch_cc/bun/blob.py`.

## The rule: never move a pristine byte

The loader's contract above is the only thing the rewrite rides. How the
builder lays the arena out, and which records follow the table, is the half of
the format that changes with Bun — and a rewrite that re-laid the arena would
have to find and re-aim every pointer in the blob, including pointers in records
it had never seen. Bun owes that to nobody, and it is where this tool broke
twice: 2.1.246 (Bun 1.4.1) chained records after the table, and a chain-blind
compaction shipped a binary that segfaulted in Bun's graph loader while every
module compared equal; 2.1.269 (Bun 1.4.3) chained two more, and the chain
walker written after 2.1.246 — mirroring Bun's reader record for record, with a
whitelist of the bits it knew — refused the build outright. Mirroring the
reader more completely is a repair that can never be finished.

So `rewrite` moves nothing. An edited module's text is appended after the
arena (NUL-terminated, as Bun's own `count_z` payloads are); the module table
and everything after it are copied verbatim, shifted by a multiple of 128 so
every phase inside them holds; then the pointers that name what changed are
re-aimed:

- the edited module's `contents` pair, at its text;
- its `bytecode` pair, at nothing ([below](#the-bytecode-and-why-we-unlink-it));
- its source-hash word, to 0 — upstream's own "none, compute it" — because the
  hash keys the text in JSC and the pristine text's hash must not describe ours;
- the offsets struct's `modulesPtr` and `argvPtr`, along with the bytes they
  name.

Every other byte, and every other pointer, is exactly where it was. A record
patch-cc has never heard of comes through the same copy and still points where
it did, because what it points at did not move; the arena's payloads keep their
128-byte phase — Bun deserializes bytecode in place and calls misalignment "a
runtime assertion error or segfault" — because the arena is not shifted at all.

What has to be *known* is exactly what is touched: the offsets struct, two
pairs and the loader byte of a module record, and the one record that
describes a module's text — its hash, first after the table when bit 5 of
`flags` says there is one. The rest of `flags` travels verbatim, bits this code
has never seen included, with one exception. Bit 4 declares every module's
text to lie in one contiguous run: Bun's runtime hints the kernel to drop those
pages after startup, and its comment forbids any other region inside the run.
The appended text is outside it, so the bit is cleared — Bun documents the
absent flag as an older layout it reads — rather than left asserting something
false.

## The records after the table

Bun ≥ 1.4.1 chains optional records directly after the module table, each
announced by a `flags` bit and read back in flag order. What has been seen so
far, for orientation — patch-cc parses none of it beyond the first:

| bit | record | since |
|---|---|---|
| 5 | `[u32; modules]` — each module's WTF hash of its source text (0 = none) | 2.1.246 |
| 6 | `u32 count`, then `count` × `{u32 id, ptr}` — internal-module bytecode | 2.1.246 |
| 7 | one pointer: the shared bytecode string table every chunk's bytecode indexes | 2.1.246 |
| 8 | `u32` — how many leading modules load before the first `import()` | 2.1.248 |
| 9 | one pointer: the string table `moduleInfo` bodies index | 2.1.248 |
| 11 | one pointer to a pre-linked ES module graph, then `u32 count` and `count` × `u32` file index | 2.1.269 |
| 12 | `u32 flags, u32 value` — runtime options (a JIT policy) | 2.1.269 |

Three shapes across the 17 published builds from 2.1.246 to 2.1.269, and none
of them a code change here: each is bytes in the tail and payloads in the
arena, and both are copied where they are. The one record whose *meaning* the
rewrite leans on is bit 11's: a module in the pre-linked graph ships no
`moduleInfo` body, and its imports and exports are read from the graph rather
than its text — which holds for an edited module too, because a patch never
touches module linkage (the syntax gate reads past it,
[PLAYBOOK.md](PLAYBOOK.md#the-many-module-surface)).

## The 2.1.242 split, and the patchable surface

Through 2.1.241 the entrypoint module *was* the app: one ~28 MB `contents`
carrying every line patch-cc anchors on. 2.1.242 turned on Bun code-splitting
with lazy loading, and the shape changed under the tool:

| build | modules | entrypoint `contents` |
|---|---|---|
| 2.1.241 | 11 | 28,249,679 bytes (the whole app) |
| 2.1.243 | 1,385 | 19,952 bytes (an argv shim) |

The entrypoint is now a ~20 KB shim that parses argv and lazily
`import()`s the app across ~1,300 `/$bunfs/root/chunk-*.js` modules; ~46 MB of
JS, the largest chunk 7.3 MB. The code did not disappear — every anchor is still
in the binary — but it left the one module patch-cc used to read, and it does not
concentrate in a single chunk (`branding` spans a dozen modules, `org-label` ten,
`spinner-tips` six).

So the patchable surface is **every module the container declares to be
JavaScript**, discovered the way the entrypoint itself is discovered — off the
artifact, never hardcoded. A module's *loader* (the second trailing flag) is how
Bun decides whether to compile it as source or hand it over as opaque bytes, and
the entrypoint is by definition the JS Bun runs, so its loader *is* the JS loader
(`Blob.js_modules`). The asset modules (the native addons, the bundled
`mermaid`/`hljs`, the HTML template) carry other loaders and are left alone. A
pre-split build is the one-module case of this — `js_modules()` returns just the
monolith — so [`js.Source`](PLAYBOOK.md#the-many-module-surface) spans one module
or a thousand through the same code.

The entrypoint still matters for one thing: it is where the manifest lives and
what `status` reads, named by the offsets struct's `entry_point_id` — the same
index Bun resolves it by. Its *name* is upstream's to change and we never read
it: 2.1.229 renamed it `/$bunfs/root/src/entrypoints/cli.js` → `/$bunfs/root/cli`.

## The bytecode, and why we unlink it

Modules carry precompiled Bun **bytecode**. Before the split only the entry
module had any (~half of the binary); the code-split builds carry it on nearly
every chunk (78 MB of 219 on 2.1.268, across ~1,650 modules), and the records
after the table name another 14 MB — the internal modules' bytecode and the
shared string table — that no edit touches.

Bun runs a module's bytecode in preference to its source, so an edited module's
bytecode would run the *unpatched* code. `rewrite` therefore aims the edited
modules' `bytecode` pair at nothing, and Bun compiles those modules from source
at launch; every untouched module keeps its bytecode and its fast start. The
stale bytes themselves stay in the file, unreferenced: reclaiming them would
mean moving everything after them, which is the compaction the rule above
forbids. A patched binary is therefore a few percent *larger* than the
original — the edited modules' text, appended — never smaller.

Measured on 2.1.268, the default patch set:

| binary | size | bytecode the table names |
|---|---|---|
| pristine | 219 MB | 78 MB, every module |
| patched (11 modules edited) | 228 MB | 51 MB, the untouched modules |

Read the current figures off any binary with `patch-cc status` rather than off
this table. Startup does not move: an edited module recompiles from source
whether its bytecode is dropped or merely unlinked, and the recompile is per
lazily-imported edited module rather than the whole app at once.

Every write asserts each **edited** module names no bytecode in the binary it
produced (`container.verify`), and `doctor`'s smoke bake writes a temp binary
through the same `container.write` and *executes* it, so the sweep exercises
the assert, and the loader itself, on every corpus build. If a future Bun build
makes bytecode authoritative over source, that assert is the tripwire — every
edit would silently no-op otherwise.

## Writing it back

`.bun` is the last *allocated* ELF section; only non-allocated metadata
(`.comment`, `.symtab`, `.strtab`, `.shstrtab`) follows it. patch-cc rewrites
the ELF bytes in place:

1. Splice the new, larger blob over the old `.bun` bytes.
2. Shift `e_shoff`, `e_phoff`, and the trailing non-alloc sections/segments by
   the size delta, rounded up to the strictest alignment among them.
3. Grow the containing `PT_LOAD` segment's `filesz`/`memsz` to match.

`.bun` keeps its original file offset. This is deliberately *not* done with a
general ELF library: LIEF rebuilds the binary and relocates `.bun` so its file
offset equals its virtual address (`0x20000000`), which inflates the file to
~715 MB. Raw in-place surgery avoids that entirely.

Guards refuse anything that could corrupt the mapping: allocated sections after
`.bun`, growth into a header table, an unrelated spanning segment, or a
misaligned `PT_LOAD` shift. If any fires, the write aborts rather than guesses.

Code: `src/patch_cc/bun/elf.py`. macOS uses LIEF (`macho.py`): growing a
Mach-O segment is page-aligned and bounded, with no relocation pathology, and
growth is the only thing the rewrite ever asks of a container, so the two
platforms behave the same way. Every edit is followed by an ad-hoc `codesign`
(mandatory on Apple Silicon).

## The manifest

Every patched bundle carries a single comment line — appended to the **entry
module**, the one module always present and always re-extracted, and the one
`status` reads — describing its shape; [PLAYBOOK.md](PLAYBOOK.md) covers what it
means for matcher health:

```
//patch-cc {"v":1,"tool":"<version>","patches":[...],"brand":...,"suffix":...,
            "models":{...},"org":...,"codex":{"port":8817,"models":["gpt-5.6-sol"]}}
```

Every key after `patches` is a configurable patch's own, declared in one place
(`Patch.setting`) so the manifest here, the cache, and the menu cannot spell it
three ways. Each is written only when *that* patch landed **and** has a value
worth recording, so `status` can never assert a name, marker, or model the
bundle does not contain — and so this is the *widest* the line gets, not its
fixed shape.

That line is why `patch-cc status` can name exactly what is applied: several
patches are value flips (`verbose:!0`) that leave no other trace. A comment
can't collide with code and travels with the bundle through extract/repack.
The menu also reads it to pre-select the current patch set — the binary is the
state.

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
- Patching always starts from that pristine copy, so re-applying never stacks
  edits on edits, and an apply where **nothing lands** — every selected patch
  broken, so the manifest would claim nothing — leaves the binary untouched
  entirely (unlinking bytecode for nothing would only slow startup). A patch
  that *lands* still writes even where it changed no bytes, because landing
  includes an override the build already satisfies: the manifest records what
  was asked and verified present, so `status` can report it.
- The bundle is parsed, and any syntax error aborts before the binary or the
  backup is touched — the two checks below answer "did we write what we meant
  to", which a corrupt splice satisfies perfectly. The parse is the same one
  the patches locate with, so it costs nothing extra and sits at *every* batch
  of edits as well as on the final bytes: a patch that produces rubble is named
  and dropped rather than aborting the run. See
  [PLAYBOOK.md](PLAYBOOK.md#the-syntax-gate).
- Every write is verified: patch-cc re-extracts the JS from the binary it just
  wrote and asserts every module equals what it meant to write, and that the
  blob is the pristine blob plus exactly the intended edits — the arena
  byte-identical in place; the module table and the tail verbatim one shift
  along, except each edited module's two pairs and hash word; the flags less
  the contiguity bit; the same entry module and the same `compileExecArgv`.
  Nothing is enumerated, so a record this code has never parsed is covered by
  the same comparison as the ones it has. The check exists because 2.1.246
  failed *only* there: every module compared equal while the written binary
  was dead.
- Patching a binary that is already marked, when no pristine backup exists, is
  refused outright — there is nothing clean to start from, and our edits change
  lengths, so a second pass would corrupt rather than update. `restore` or a
  reinstall are the only honest fixes; there is deliberately no override.
