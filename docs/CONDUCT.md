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
  are its whole input; no saved state may silently change what it does, so the
  same command always yields the same result. Persisted choices belong to the
  interactive UI alone — they pre-fill a prompt, never trigger an action.

- **Anchor matchers on meaning.** String literals, `case` labels, prop names,
  control-flow shape — never a minified local that changes every build. A new
  upstream shape earns a narrow new branch, not a looser regex. Full rules and
  the repair loop: [PLAYBOOK.md](PLAYBOOK.md).

- **Report absent apart from broken.** A matcher that finds nothing may be a
  shape this build simply lacks — most patches carry several — not a regression.
  Keep "gone", "already applied", and "not on this build" as distinct signals;
  never collapse them into one number.

- **Port faithfully.** When you change a patch, verify its output against the
  upstream reference on a real bundle — byte-identical where behaviour must not
  change. The JS→Python porting traps are in [PLAYBOOK.md](PLAYBOOK.md).

- **The user controls commits and releases.** Don't commit, push, or publish
  unless asked.

The binary format, and why the ELF write is in-place and the bytecode is
dropped: [INTERNALS.md](INTERNALS.md).
