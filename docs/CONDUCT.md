# CONDUCT.md — patch-cc

How we build here. What the project *is* and how to use it live in the
[README](../README.md); this file is the *how*, not the *what*. Read it before
touching a matcher or the container layer.

## Mindset

Every change reaches for the **minimal, elegant, graceful** form — the simplest
shape that already absorbs every case, found rather than bolted on.

- **Grace, not branches.** Dissolve edge cases into the common path instead of
  guarding them with an `if`. Empty, missing, already-applied, absent-on-this-
  build should flow through the *same* code as the normal case. A special-case
  branch you could dissolve is a miss, not a smaller win.
- **DRY — one source of truth.** Every value, rule, and fact has one home;
  everything else links to it. This holds for the docs too: if it's in the
  README, don't restate it here. Two copies drift, and the reader can't tell
  which one is true.
- **Cut, don't accrete.** Keep the smallest surface that does the job. Delete
  superseded code, flags, and comments in the same change; add no abstraction
  for a caller that doesn't exist yet.

## Guidelines

- **Never corrupt the user's binary.** Writes are staged, re-extracted, and
  verified byte-exact before replacing the original, and a pristine backup
  always exists for `restore`. A bug should leave a working `claude`, never a
  brick.

- **One input, and the menu is its editor.** `apply` bakes the saved selection
  — the file the menu writes — and nothing else: no flag, no environment, no
  guess. The menu and `apply` read it through the same `cache.seed`, so what
  the menu shows is what `apply` bakes, and a successful bake writes it back,
  so the file always describes the last one. What a build cannot honour is
  said and skipped, never silent; a file that will not parse is refused by
  `apply` (which acts) and shown over the binary's own state by the menu
  (which only shows). Nothing contacts an endpoint to bake: discovery only
  suggests ids in the menu. A key never enters the binary, the manifest or the
  selection.

- **Find by the name upstream wrote; edit the grammar node.** The authored
  names — string literals, `case` labels, property names — and the shape of the
  tree are what a build keeps. Never describe the syntax between them, and never
  anchor on a minified local: that is the half a minifier regenerates. A new
  upstream *shape* earns a narrow new branch; a new *spelling* of one shape
  should already cost nothing. Full rules and the repair loop:
  [PLAYBOOK.md](PLAYBOOK.md).

- **Report absent apart from broken.** A matcher that finds nothing may be a
  shape this build simply lacks — most patches carry several — not a regression.
  Keep "gone", "already applied", and "not on this build" as distinct signals;
  never collapse them into one number. Which one a sub-step's silence means is
  not guesswork: declare it (`Outcome.declare`) so a green tick cannot cover a dead
  feature. A step nobody declared is a step that cannot report its own death:
  make a name badge's boldness conditional upstream and an undeclared badge
  step is a green run, an unchanged banner, and a manifest asserting the new
  name. And a step answers for its own identity alone, never for a name another
  step discovered: one shape that moves (2.1.257 compiled the transcript
  renderer's memos into cache slots) would otherwise read as every step that
  leaned on it *finding nothing*, and the report would point away from the one
  thing that moved. See [PLAYBOOK.md](PLAYBOOK.md).

- **Ride what upstream ships working.** When the binary already does for one
  kind of thing what a patch wants for another — live tool uses, for live
  thinking — put the patch's data on that path instead of building a twin of
  it. Every step of a parallel implementation is a claim only the patch needs,
  and upstream owes it nothing; the path upstream ships is one it cannot break
  without paying for it. `live-thinking` is three insertions on the tool-use
  list, where a render path of its own would owe a claim per React idiom
  upstream respells ([PLAYBOOK.md](PLAYBOOK.md#live-thinking--streamingpy)).

- **Never move a byte you did not write.** The container is the second worked
  case of the rule above. Bun reads a payload through its pointer and ignores
  what nothing points at; a rewrite that re-lays the arena has to re-aim every
  pointer in the blob, including ones in records it has never seen, and Bun
  adds such records whenever it likes — 2.1.246 (Bun 1.4.1) chained three after
  the module table, 2.1.248 and 2.1.269 two more each — so a whitelist of the
  known ones can never be finished. So the write appends what changed and copies
  everything else where it was, and needs to know only what it touches
  ([INTERNALS.md](INTERNALS.md#the-rule-never-move-a-pristine-byte)). The price
  is a binary a few percent larger instead of smaller; a size win would be the
  claim that bought the fragility.

- **Port faithfully.** When you change a patch, verify its output against a real
  bundle — byte-identical where behaviour must not change. `doctor` over the
  archived corpus is that check, and the *diff* between two sweeps is the half
  that matters: a red build is loud on its own, but a widened locator shows up
  only as an old build's counts quietly moving. Be able to say what every moved
  number means. The sweep is in [PLAYBOOK.md](PLAYBOOK.md).

- **The user controls commits and releases.** Don't commit, push, or publish
  unless asked.

The binary format, and why the write appends rather than compacts:
[INTERNALS.md](INTERNALS.md).
