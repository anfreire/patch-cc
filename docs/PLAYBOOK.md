# Patch playbook

For maintainers. How the patches are built to survive Claude updates, and how to
repair one when a build breaks it.

This is a Python port of
[a-connoisseur/patch-claude-code](https://github.com/a-connoisseur/patch-claude-code);
that repo's git history is the best archive of how each matcher has drifted over
time.

## Matcher rules

The minified bundle is regenerated on every Claude build, so identifiers churn
constantly. These rules are what keep matchers alive:

- **Never** anchor on a minified local (`A_`, `mET`, `wg6`). Anchor on string
  literals, `case` labels, prop names, or control-flow shape.
- Match the *semantic shape* of a function body, not its symbol names.
- When upstream ships more than one shape for the same feature, add a second
  narrow branch — don't widen one regex until it over-matches.
- Accept statement variants a minifier flips between. The thinking gate broke
  on 2.1.216 solely because `if(x)return null;` became `if(x){return null}` —
  matchers should allow both from day one.
- Compile with `re.ASCII` (helper `compile_js`) so `\w` behaves like JS.
- JS `.replace(re, fn)` without `/g` replaces **once** → `re.sub(..., count=1)`.
  JS `.replace("a","b")` on a string also replaces once → `str.replace(a, b, 1)`.
  Getting this wrong over-rewrites.
- Always pass a *function* replacement to `re.sub`, never a template string
  (backslashes and `\g<>` would be interpreted).

## Discovery instead of hardcoding

Anything the binary can enumerate for us, it does:

- **Built-in agents** are found by the definition shape
  `agentType:"<name>",whenToUse:` carrying `source:"built-in"` in the same
  object (`agents.discover_agents`). Definitions whose `whenToUse` begins with
  `"Internal` are plumbing and are not offered.
- **Model aliases** come from the Task tool's own input schema: the
  `model:<zod>.enum([...])` whose describe-string starts
  `Optional model override` (`agents.discover_models`).

A new upstream agent or model appears in `patch-cc list`, the menu, and
`--model` validation without a code change. If the enum anchor ever vanishes,
discovery falls back to `haiku/sonnet/opus` — `doctor` prints both lists, so a
missing agent or alias is visible at a glance.

## The manifest

Every patched bundle ends with one comment line:

```
//patch-cc {"v":1,"tool":"0.1.0","patches":[...],"brand":...,"models":{...}}
```

A comment cannot collide with code, survives re-extraction, and makes `status`
a parse instead of a guess — several patches are value flips
(`verbose:!0`) that leave no other fingerprint. `is_patched` also still
recognises the legacy fingerprints (`__cc_` identifiers, the old `--version`
marker) so binaries patched by pre-manifest versions are not mistaken for
clean.

## How resilience is detected

`patch-cc doctor` runs every patch against a **clean** bundle (the pristine
backup if the installed binary is already patched) and reports, per patch:

- `candidates` — how many times the anchor/shape was found
- `applied` — how many rewrites actually changed something

Configurable patches are fed a synthetic configuration built from the bundle's
own discovered agents and models (every agent assigned a model different from
its current one, a test brand), so branding and the model overrides are
exercised for real — nothing is exempt from the dry run.

Two failure modes, kept distinct:

| symptom | meaning |
|---|---|
| `candidates == 0` | the anchor is **gone** — a real regression |
| `candidates > 0, applied == 0` | shape found, rewrite was a no-op — usually already patched |

`doctor` prints the documented anchor counts for any broken patch, so a `0`
next to an anchor points straight at what moved.

## Sub-steps, and why `live-thinking` has them

Most patches are one rewrite. `live-thinking` is ~11 named sub-steps, because
upstream has reshaped the stream reducer at least three times and a single hit
count cannot tell "all landed" from "half silently drifted".

Each sub-step records its own `candidates`/`applied`. Sub-steps come in two
kinds:

- **Independent fixes** (`memo-cache`, `linger`, `display-mode`, `bottom-row`,
  …) — each may or may not be present on a given build.
- **Mutually-exclusive reducer variants** — `reducer-destructured` (2.1.138+),
  `reducer-inner` (2.1.183+), `reducer-legacy` (pre-2.1.138). On any one build
  exactly one should land. On 2.1.216 it is `reducer-inner`.

A sub-step that finds nothing (`candidates == 0`) is reported as *absent*, not
broken — it is just a shape this build doesn't have. A sub-step that finds a
shape but fails to rewrite it (`candidates > 0, applied == 0`) is the real
concern and shows up in `missed_steps()`.

If **every** reducer variant is absent, live thinking won't work — that is the
signal that upstream shipped a fourth reducer shape that needs a new variant.

## Repairing a broken patch

1. Get a clean bundle from a current binary:

   ```bash
   patch-cc extract ~/.local/share/claude/versions/<ver> > clean.js
   ```

2. Run `patch-cc doctor`. Note which patch dropped to `candidates == 0`, or —
   for `live-thinking` — which sub-step.

3. Search the clean bundle for the *semantic* anchor, not the old identifier:

   ```bash
   rg 'case"collapsed_read_search"|case"thinking_delta"|spinnerTipsEnabled|Backgrounded agent' clean.js
   ```

4. If the anchor moved, find the new shape and update the matcher in the
   relevant `src/patch_cc/patches/*.py`. Prefer adding a branch over loosening
   the existing regex.

5. Re-run `doctor` until the patch (and each expected sub-step) is green, then
   apply to a real binary and check the behaviour at runtime.

## Patch reference

Grouped as in `patch-cc list`. Each entry: what it changes, the stable anchor,
and where it lives.

### Output & diffs — `output.py`

- **`tool-calls`** — force verbose collapsed read/search rows.
  Anchor: `case"collapsed_read_search"`.
  *Value-flip* (`verbose:!0`) — the manifest is its only fingerprint.
- **`create-diff`** — render created files through the diff renderer with `+`
  lines. Anchors: adjacent `case"create":` / `case"update":`; the update arm
  must expose `structuredPatch`.

### Thinking — `thinking.py`

- **`thinking-inline`** — make historical thinking blocks render inline.
  Anchor: `case"thinking":` containing `isTranscriptMode:`. Two rewrites:
  remove the early null-return (both `if(!a&&!b)return null;` and the 2.1.216
  block form `if(!a&&!b){return null}`), then force `isTranscriptMode:!0`
  (and `hideInTranscript:!1` where present) in the renderer props. The
  component itself has no gate — an empty summary renders nothing, which is
  why trivially short thinks may still show no block.

### Live thinking — `streaming.py`

- **`live-thinking`** — the ~11-step patch above. Discovery anchor is
  `onStreamingThinking:` → `useState(null)` (the older `hidePastThinking`
  anchor is gone as of 2.1.216 — the fallback back-scan is load-bearing).
  Reducer anchors: `type==="stream_request_start"`, `case"thinking_delta"`,
  `content_block_start`.
  The `display-mode` sub-step defaults the request's thinking display to
  `"summarized"`; without it the API only streams summary text when the
  `showThinkingSummaries` setting is on. Two shapes: the legacy inline env
  check, and the 2.1.216 form that hoists
  `X=qt(process.env.CLAUDE_CODE_DISABLE_THINKING)` and gates the display
  behind extra feature-helper calls (kept verbatim by the matcher).

### Subagents — `agents.py`

- **`subagent-prompt`** — show the Prompt block outside transcript mode.
  Anchor: `"Backgrounded agent"` + `action:"app:toggleTranscript"`.
- **`subagent-models`** — write the chosen model into each overridden built-in
  definition (discovered as above): rewrite the `model:"..."` literal when the
  definition has one, insert `model:"...",` right after `agentType:"...",`
  when it doesn't. Both splice at offsets from a fresh discovery pass.
  **The bypass:** one helper ignores the definition's model for a single
  pinned agent (Explore today) — shape
  `function f(def,main){if(def.agentType!==X.agentType||def.source!=="built-in")return def.model;return g(main)?PIN:"inherit"}`.
  When the pinned agent (resolved by following `X` back to its
  `X={agentType:"..."}` assignment) is among the overrides, the body is
  rewritten to `return def.model`. Without this, Explore ignores every
  override at runtime — the literal is written but dead.

### Chrome & branding — `chrome.py`

- **`spinner-tips`** — force spinner tips off. Anchors: `spinnerTipsEnabled===!1`
  guard and `spinnerTipsEnabled!==!1` expression (two paths). *Value-flip.*
- **`version-marker`** — append `\n<suffix>` after `}.VERSION} (Claude Code)`
  (default `(patched)`, customisable via `--suffix`; escaped for the template
  literal it lands in).
- **`branding`** — rename visible `Claude Code` startup/help strings to a
  chosen name (default: `<username>'s Code`). Several string shapes, each its
  own sub-step. Opt-in via `--brand`.

## Removed patches

Kept here so nobody reintroduces them without knowing why they left:

- **`word-diff-bg`** — as of 2.1.216 the word spans are nested inside a row
  element that already carries the line background; the fallback could never
  change a pixel. Confirmed redundant in live A/B.
- **`installer-label`** — its target string left the bundle in ~2.1.186.
- **`redacted-thinking`** — untestable against the real API (no way to elicit
  a `redacted_thinking` block), and the native-only tool keeps its surface to
  what can be verified.
