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

- **Explicit invocations are hermetic.** A non-interactive command's arguments
  are its whole input; no saved state may *silently* change what it does.
  Persisted choices pre-fill the interactive UI and never act on their own. Two
  flags do read state, and each *names* the state it reads: `apply --from-cache`
  takes the remembered selection, and `apply --codex` takes your plan's
  catalogue — asking for a Codex model is asking for the plan that describes it,
  so the display name and context window are read from there rather than
  invented. Neither is hidden behind a default, and absent both, the same command
  always yields the same result. Asking for a Codex model is also the one thing
  whose *output* can differ between two identical invocations — under either flag
  that does it, `--codex` naming ids directly or `--from-cache` replaying a
  remembered set that holds them (both re-read the plan at bake time): a plan
  that cannot be reached is not an error, so the id still bakes, on its
  fallbacks. That is the price of not guessing a context window, and it is paid
  under flags that say so.

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
  `branding` carried none, so a name badge that stopped being a bold render was
  a green run, an unchanged banner, and a manifest asserting the new name. And a
  step answers for its own identity alone, never for a name another step
  discovered: `live-thinking`'s reducer once skipped itself for want of a helper
  the memo step had found, and eight required steps read *found nothing* over
  one memo that had merely moved (2.1.257). See [PLAYBOOK.md](PLAYBOOK.md).

- **Ride what upstream ships working.** When the binary already does for one
  kind of thing what a patch wants for another — live tool uses, for live
  thinking — put the patch's data on that path instead of building a twin of
  it. Every step of a parallel implementation is a claim only the patch needs,
  and upstream owes it nothing; the path upstream ships is one it cannot break
  without paying for it. `live-thinking`'s render half broke five times on
  React idioms before it was cut to three insertions on the tool-use list
  ([PLAYBOOK.md](PLAYBOOK.md#live-thinking--streamingpy)).

- **Never move a byte you did not write.** The container is the second worked
  case of the rule above. Bun reads a payload through its pointer and ignores
  what nothing points at; a rewrite that re-lays the arena has to re-aim every
  pointer in the blob, including ones in records it has never seen, and Bun
  adds such records whenever it likes — 2.1.246 corrupted one, 2.1.269 was
  refused over one, and the whitelist of known records between the two could
  never be finished. So the write appends what changed and copies everything
  else where it was, and needs to know only what it touches
  ([INTERNALS.md](INTERNALS.md#the-rule-never-move-a-pristine-byte)). The price
  is a binary a few percent larger instead of smaller; the size win was the
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
