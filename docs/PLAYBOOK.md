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
- Match an optional brace **pair** conditionally (`(\{)?…(?(1)\})`), never as
  two independent `\{?` / `\}?`. A lone optional closing brace eats the
  *enclosing* block's `}` on the unbraced shape, and a rewrite that emits its
  own leaves the bundle unbalanced — with `candidates`/`applied` unchanged, so
  nothing looks wrong until Claude fails to start.
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

### Expectations — why a green tick means something

Counting alone cannot tell "this build lacks that shape" from "the feature is
dead": a patch whose optional shapes rewrite happily while a load-bearing one
is gone still changes bytes, and would read as green. So each sub-step declares
what its absence *means* (`Outcome.step(..., expect=...)`):

| mark | meaning | absence is |
|---|---|---|
| *(default)* | a shape only some builds carry | informational |
| `expect=True` | the patch does nothing useful without it | a regression |
| `expect="<group>"` | alternate shapes; at least one must land | a regression |

`Outcome.unmet()` turns a violation into a sentence (`required step
group-routing found nothing`); `Outcome.failures()` adds any exception the
patch raised, because a patch that threw and a patch that missed an
expectation are the same verdict wearing different clothes; `Outcome.health`
folds them into `ok` / `partial` / `broken`. Every surface (`apply`, `doctor`,
the menu) reads *those two* and never re-derives either — `doctor` once judged
on counts alone and printed a red cross and "all patches still match" in the
same report. Adding a sub-step means deciding which row of the table it is in;
that decision is the whole safety net.

Two rules keep the net from having holes:

- **Count what the rewrite *achieved*, not that some rewrite happened.** A step
  built from many literal edits lands as soon as *one* of them applies, so an
  incidental edit can vouch for the essential ones. `live-thinking` learned this
  the hard way: a reducer whose setter threading applied while every event arm
  had drifted reported hits and streamed nothing. The two edits that *are* the
  feature are now checked by the markers their builders emit
  (`streaming._CORE_UPDATES`), which no amount of neighbouring churn can fake.
- **Declare an expectation before the work, not inside it.** A step created only
  by its own success cannot report its own absence. `_live_thinking` registers
  the core updates up front; `agents.bypassed_agents` resolves the pinned agent
  from the helper's durable head so a drifted body still has a step to fail.

`apply` acts on the verdict: a broken patch is re-run out of the final pass, so
its orphan edits never reach the binary, the manifest never claims it, and the
command exits non-zero. The healthy patches still apply.

That re-run is a **fixpoint**, not one retry. Patches see each other's output,
so dropping one can change what the next finds; the set is settled only when a
whole run comes back with nothing broken, and each patch is reported by the last
run it took part in. Judging the bytes of the final run by the verdicts of the
first is how a manifest starts lying. The manifest is held to the same rule:
`brand`, `suffix` and `models` are recorded only when *their* patch landed, so
`status` can never assert a name the bundle does not contain.

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

An *optional* sub-step that finds nothing is reported as absent, not broken —
it is just a shape this build doesn't have. A sub-step that finds a shape but
fails to rewrite it (`candidates > 0, applied == 0`) shows up in
`missed_steps()`.

The rest carry expectations, so their absence is checked rather than noted:

- **Required** — `prop-threading`, `display-mode`, `transcript-signature`,
  `inline-extras`, plus `block-start` and `thinking-delta`. Each is a link in
  the chain from stream event to rendered row; without any one of them live
  thinking is dead however many other steps land. The last two are not matchers
  but *proof*: they are credited only when the rewritten reducer body actually
  contains the state updates, which is what recognising a reducer does not by
  itself establish.
- **The `reducer` group** — `reducer-destructured`, `reducer-inner`,
  `reducer-legacy`. At least one must land; none landing is the signal that
  upstream shipped a fourth reducer shape that needs a new variant. Two landing
  is allowed on purpose — a transitional build carrying two reducers would have
  both correctly patched, and that is no reason to cry wolf.
- **Optional** — `memo-cache`, `memo-removal`, `linger`, `bottom-row`,
  `final-summary`. The first four match nothing on 2.1.216+ and are kept for
  older builds; `discover` is a notes-only channel that records how far back the
  state back-scan reached, which is the early warning for `_DISCOVER_WINDOW`
  (35,116 of 50,000 on 2.1.217).

Notes print on every run, green ones included — an early warning held back
until something breaks arrives too late to be one. Absences are the noisy half
(most patches lack several shapes on any build) and wait for a verdict that is
not `ok`. Both surfaces draw the same list (`ui.findings`): the CLI and the menu
each worded their own once, and the menu's copy had quietly lost the exception
that broke a patch along with every note.

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

6. Sweep the fix over the versions you still have. `doctor` takes a path, and
   every binary patch-cc has ever touched left a pristine copy in
   `~/.local/share/patch-cc/backups/`:

   ```bash
   for b in ~/.local/share/patch-cc/backups/*.orig; do
     echo "== $b"; patch-cc doctor "$b" || true
   done
   ```

   That is what keeps "add a narrow branch" honest: a widened regex that
   over-matches an older build shows up here instead of in a bug report.

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

- **`thinking-summaries`** — stop echoing the account's server-side experiment
  bucket, so the API returns thinking blocks with text in them.
  Anchor: the `?.atis` read plus the getter's whole return shape, the read tied
  to both its uses by backreference (header name: `x-cc-atis`). Matching the
  property and then replacing a brace-free body would also hit any *other*
  function reading it, deleting whatever else that one did; one `.atis` per
  bundle is today's happenstance, not an invariant. Candidates are counted off
  the header name, not the matcher, so a reshaped getter reads as
  `candidates > 0, applied == 0` — a matcher to repair — instead of the zero
  that would equally mean upstream retired the mechanism.
  Claude Code caches a GrowthBook assignment (`clientDataCacheSlots[...].atis`
  in `~/.claude.json`, one slot per account × entrypoint × model) and replays it
  to the API on every request so the server applies the same bucket. A slot in a
  bucket that withholds thinking summaries is served thinking blocks carrying a
  signature and an **empty string** — no `display` mode, effort level or
  `thinking.type` changes it, and two requests that differ only in the header
  differ in nothing else. Because the slot is per model too, one model can think
  visibly while another stays blank in the same session. Every other thinking
  patch then
  renders that empty string faithfully, which is why the symptom reads as
  "thinking works on one account and not another, same binary, same config".
  The getter is read in exactly one place, to set that one header, so emptying
  it lets the header's existing `if(value!==void 0)` guard skip it — nothing is
  sent, and no branch was added to stop it. Mind the breadth: this drops the
  bucket for *every* experiment the account is enrolled in, not just the one
  that empties thinking. Local feature values still come from the on-disk cache,
  so only the server's view of the assignment changes.

  Diagnose from a transcript rather than by eye — `thinking` blocks are
  recorded whether or not they carry text:

  ```bash
  jq -r 'select(.type=="assistant").message.content[]?
         | select(.type=="thinking") | (.thinking|length)' \
     ~/.claude/projects/<slug>/<session>.jsonl | sort -n | uniq -c
  ```

  A column of `0`s is this patch missing (or a bucket it does not yet cover);
  a spread of real lengths means the text arrived and the problem is rendering.

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
  `function f(def,main){if(def.agentType!==X.agentType||def.source!=="built-in")return def.model;…;return g(main)?PIN:"inherit"}`.
  When the pinned agent (resolved by following `X` back to its
  `X={agentType:"..."}` assignment) is among the overrides, the body is
  rewritten to `return def.model`. Without this, Explore ignores every
  override at runtime — the literal is written but dead.

  The helper is matched in two pieces on purpose. Its **head** (the
  two-condition guard) identifies it and names the pinned agent; its **body**
  is what gets replaced, and upstream keeps growing it — 2.1.217 inserted a
  `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP` escape hatch between the two,
  which silently cost every Explore override until the body matcher learned to
  skip intervening brace-free statements. Resolving the agent from the head
  alone is what makes a future body reshape *loud*: we still know an override
  is at stake, so `bypass:<agent>` is a required step that fails, instead of the
  agent's identity vanishing with the match and the step never existing.

  If the **head** goes too there is no step to fail — nothing left names the
  agent — and no way to tell "upstream stopped pinning" from "the guard
  reshaped". That one is a note instead, on a patch that stays green; it is why
  notes print on green runs.

  `bypassed_agents` returns each helper's **offset**, and the rewrite is
  anchored there (`_MODEL_BYPASS.match(content, at)`). Identifying one helper
  and then rewriting whichever one a fresh search happens to find first is how
  you neutralise an unrelated helper and report success; anchoring also means a
  build that pins two agents gets both handled, rewritten last-first so earlier
  offsets stay valid.

### Chrome & branding — `chrome.py`

- **`spinner-tips`** — force spinner tips off. Anchors: `spinnerTipsEnabled===!1`
  guard and `spinnerTipsEnabled!==!1` expression (two paths). *Value-flip.*
  Each path counts candidates off the **setting name**, not its own regex, so a
  reshaped path reads as `candidates > 0, applied == 0` — a miss — instead of
  the zero that would be indistinguishable from a build that lacks it, with the
  other path carrying the patch to green and tips still showing.
- **`version-marker`** — append `\n<suffix>` after `}.VERSION} (Claude Code)`
  (default `(patched)`, customisable via `--suffix`; escaped for the template
  literal it lands in).
- **`branding`** — rename visible `Claude Code` startup/help strings to a
  chosen name. Several string shapes, each its own sub-step. On by default,
  deriving `<username>'s Code`; `--brand NAME` names it explicitly, and selects
  the patch when it is not already in the set.

## Removed patches

Kept here so nobody reintroduces them without knowing why they left:

- **`word-diff-bg`** — as of 2.1.216 the word spans are nested inside a row
  element that already carries the line background; the fallback could never
  change a pixel. Confirmed redundant in live A/B.
- **`installer-label`** — its target string left the bundle in ~2.1.186.
- **`redacted-thinking`** — untestable against the real API (no way to elicit
  a `redacted_thinking` block), and the native-only tool keeps its surface to
  what can be verified.
