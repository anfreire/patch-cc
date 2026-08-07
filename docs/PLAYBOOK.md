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
  narrow branch — don't widen one regex until it over-matches. A different
  *spelling* of one shape is not a second shape, and does not earn a branch; see
  "applied to, not reached through" below for where that line falls.
- Accept statement variants a minifier flips between. The thinking gate broke
  on 2.1.216 solely because `if(x)return null;` became `if(x){return null}` —
  matchers should allow both from day one.
- **Applied to, not reached through.** Match what a helper is applied to; say
  nothing about how it is reached. The callee of a zod schema is pure minifier
  noise — the persisted `effortLevel` reads `E.enum(` on 2.1.216, `A.enum(`,
  `b.enum(`, `v.enum(`, `w.enum(` through 2.1.223, then `Ir(` on 2.1.224 — and
  every matcher that named any of it dated itself to one build. 2.1.224 is the
  proof: `.enum([` dropped from 241 sites to 23 in a single release, because the
  spelling tracks which *module* a schema lives in (bundled namespace alias vs
  bare standalone factory), never what the schema is. So the callee is
  `base.ARRAY_CALL` — whatever sits between the prop name and the array literal,
  bounded only by statement and block edges — and no spelling is enumerated.
  Enumerating the spellings seen so far only defers the same break to the next
  one; that is the boundary of the narrow-branch rule above.
- **Dissolving the callee raises what the surviving anchors must carry**, and
  that debt is per-site, never general. `effortLevel:` plus its level array is
  unique on every build. `model:` plus an array is *not* — 6 sites on
  2.1.216–220, 12 on 2.1.221–224 — so `MODEL_ENUM`'s
  ``.optional().describe(`Optional model override`` tail is its sole
  discriminator, not belt-and-braces. Trimmed, the matcher finds three sites, two
  of them the settings-scope enum `["user","project","local"]`, and
  `codex-models` splices Codex ids into it. Measure an anchor's match count on
  every backup before removing any part of it: under the older, tighter callee
  that same probe read 1 on all 8 builds, so a measurement taken before the
  callee was dissolved does not survive it.
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
- **Model aliases** come from the Task tool's own input schema: the `model`
  enum whose describe-string starts
  `Optional model override` — `agents.MODEL_ENUM`, one compiled anchor because
  `codex-models` splices the chosen Codex ids into the very group
  `discover_models` reads back out.

A new upstream agent or model appears in `patch-cc list`, the menu, and
`--model` validation without a code change. If the enum anchor ever vanishes,
discovery falls back to `haiku/sonnet/opus` — `doctor` prints both lists, so a
missing agent or alias is visible at a glance.

**The one hardcoded list, and why it may stay one.**
`models._RESERVED` names Claude's short built-ins so `apply --codex opus` is
refused, and so a *derived* family shortcut can never take one of those names
either (a pathological `gpt-5.6-opus` is the only way it could try). It is a
snapshot, so upstream can outgrow it — and it does not matter, because it is a
courtesy, not the guard. A name the bundle already knows makes `enum` and
`validator` find it *present*, so both rewrite nothing; they are `expect=True`,
so the patch reports broken, the fixpoint drops it, and nothing is written. The
bundle refuses the collision at bake time, derived rather than remembered.
Reserving the discovered set instead would buy a friendlier message for a hazard
that cannot land, at the price of reading a 275 MB binary before argument parsing
can finish.

## The manifest

Every patched bundle ends with one comment line recording what was applied; its
shape lives in [INTERNALS.md](INTERNALS.md#the-manifest). What matters here is
that it makes `status` a parse instead of a guess — several patches are value
flips (`verbose:!0`) that leave no other fingerprint — and that `is_patched`
also still recognises the legacy fingerprints (`__cc_` identifiers, the old
`--version` marker), so binaries patched by pre-manifest versions are not
mistaken for clean.

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

Grouped by source module. The menu's three groups (Output & display, Models &
effort, Chrome & branding) are display categories that each draw from several
modules — module says where a matcher lives, group says what the patch changes
for you. Each entry: what it changes, the stable anchor, and where it lives.

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

- **`max-effort`** — let `/effort max` save as the session default, as
  low–xhigh already do. `max` is first-class in session (the binary's level
  list is `["low","medium","high","xhigh","max"]`; the CLI `--effort` and
  `CLAUDE_CODE_EFFORT_LEVEL` both accept it) — only *persistence* runs through
  two whitelists that stop at xhigh, and both are required rewrites because
  either alone changes nothing:

  - **`gate`** — one tiny whitelist function
    (`if(e==="low"||…||e==="xhigh")return e;return`), and the whole choke
    point: the `/effort` save path writes `effortLevel` to userSettings only
    when it returns a value, the startup resolver reads the setting back
    through it, and the model-picker flow persists through it too — so write
    and read widen together in one rewrite. The matcher accepts both
    minifier statement forms and the whole-body match keeps it off any other
    function that merely compares levels.
  - **`schema`** — the settings validator's
    `effortLevel:*.enum(["low","medium","high","xhigh"])`, anchored on the
    prop name (the describe-string `Persisted effort level` is the listed
    anchor). Its `.catch(void 0)` is the safety property: a **clean** binary
    reading a settings file that still says `"max"` (baked, then reverted by
    a Claude update) treats it as unset — the default effort, never a broken
    settings parse. Losing the patch costs the preference, nothing else.

  Both matchers tolerate the level already being present and count it as
  landed — an upstream that adopts `max` persistence itself is the goal
  achieved, not a miss (the same judged-on-achievement rule
  `subagent-models` follows). Effort semantics downstream are untouched: the
  org-limit clamp runs before the save, and a model that cannot run `max`
  clamps at request time exactly as an interactive `/effort max` does today.

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
  Every requested override is a **required step**: each one reaching this patch
  has already been validated against the bundle by whichever surface asked for
  it, so a pin that cannot be written is not a shape this build lacks — it is
  the asked-for change failing, and the patch is dropped rather than shipping a
  manifest that claims it. An override whose target the definition already
  carries counts as landed: the step is judged on what it achieved, not on
  whether bytes moved.
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

### Codex — `codex.py`

- **`codex-models`** — make Claude Code accept and show the chosen Codex models,
  and divert *only* those models' requests to the localhost gateway.
  Eight sub-steps; with nothing chosen the patch is a no-op, like
  `subagent-models`. The ids and the port are ordinary patch configuration
  (`Options.codex_models` / `codex_port`) — chosen in the menu or with
  `--codex`/`--codex-port`, never read from a store of the patch's own.

  | step | what it changes | anchor |
  |---|---|---|
  | `enum`\* | the Task tool's `model` enum, so a subagent can be pinned to a Codex id | `agents.MODEL_ENUM` — the same anchor `discover_models` reads |
  | `validator`\* | the known-model array — it gates *resolution*, not just acceptance | `["sonnet","opus","haiku","fable",…,"opusplan",…]` |
  | `resolver`\* | the override resolver `J9n` | its `case"best":{…}` block |
  | `general-resolver`\* | the resolver every ordinary request uses, `Ei` | `case"best":return X();default:}` |
  | `redirect`\* | swaps the request origin to `127.0.0.1:<port>` | the SDK's `buildRequest`, up to `let u=this.buildURL(…)` |
  | `picker` | the `/model` list | `?[n,r]:[r];for(let i of o)push(e,i,t);` |
  | `context` | the real context window | the brace-free `(e,t)` body reading `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
  | `registry` | the binary's own model table — the status-line name, effort capabilities, `/advisor` eligibility | `],aliases:{` closing the `models:[...]` registry |

  \* required (`expect=True`) — without any one of them the feature is dead.
  `picker`, `context` and `registry` are refinements: absent, you can still
  type `/model <id>` and get the 200k default under the model's raw id.
  `context` has no step at all when no chosen model reports a window — there
  is no rewrite owed, and reporting that as either a missing shape or a missed
  rewrite would blame the build for having nothing to do.

  **An id is a model's whole identity.** There is no second name to carry: the id
  is what `--codex` takes, what the manifest records, what the enum and the
  picker and the redirect array hold, and what a diverted request already names
  by the time the gateway sees it. Everything else is derived — the display name
  and the window from the plan, the shortcut from the id — so nothing downstream
  keeps a mapping that could disagree with the bundle. That is why the gateway
  needs only the port, and it is structural rather than a rule to remember:
  there is no map left to read.

  **The picker's label rule is one rule.** Every row's label is its handle
  *spelled as a name* — `_display_name` turns `sol` into `Sol` and
  `gpt-5.6-sol` into `GPT 5.6 Sol` — because the binary's own rows are named,
  not slugged. Labelling id rows with the raw id instead made them the only
  entries in the list wearing a different sort of name than their neighbours
  (`gpt-5.6-sol` sitting under `Opus`). Two label rules is what let that
  through, so there is one.

  **The registry is the binary's own model table, and the sharpest splice
  here.** Everything Claude Code knows about a model it did not hardcode a
  check for lives in one embedded object — `models:[{id, family, display_name,
  provider_ids, context, capabilities:[...], default_effort, advisor_rank,
  ...}]` plus the `aliases` map its resolvers read — validated by a zod
  `safeParse` whose fallback is **empty** (`models:[]`): one malformed entry
  strips every model, Claude's included, of its metadata. So the step emits
  only fields the schema declares — the required four (`id`, `family`,
  `display_name`, `provider_ids.first_party`) plus values with something true
  to record. `default_effort` is deliberately not among them: the binary
  resolves a missing default as `high` (`?.default_effort??"high"` — the very
  default its own flagships declare), so omission makes `/effort auto` and an
  untouched session mean on these models exactly what they mean on Opus,
  instead of importing Codex-the-product's own default (`low` on sol). An
  entry is what turns "accepted" into "first-class": the status line resolves
  `display_name` through it (raw id without one), `/advisor` eligibility is
  exactly "has an `advisor_rank`", the effort gates read `capabilities`, and
  surfaces nobody has enumerated inherit the same answers. `/advisor sol` on a
  freshly baked binary is the cheap end-to-end check that the entry parsed.

  **What the entry deliberately leaves out.** `pricing` and
  `max_output_tokens` (readers guard for absence; a subscription has no
  per-token price; the cap never reaches the wire), `context` (the window
  resolver ends on a flat 200k without consulting the registry — the `context`
  step stays the one home for the window), and the `aliases` /
  `latest_per_family` maps (shortcuts already resolve through the spliced
  arms; a second mechanism would be a second home — and `latest_per_family`
  feeds Claude's own system-prompt text). The `/model` picker does not iterate
  the registry either — its rows are hand-built per family upstream, which is
  why `picker` exists — and our row push is the same `{value,label,description}`
  + `.some()` idiom the binary itself uses for `ANTHROPIC_CUSTOM_MODEL_OPTION`.

  **Capabilities can only say yes.** The binary reads an *absent* capability
  as "ask the provider fallback", which on the first-party API is permissive —
  upstream's own choice for models it does not know. That is why the `/effort`
  menu offers the whole ladder on any imported model, and why an entry cannot
  *hide* a level; hiding would mean splicing the per-level exclusion chains,
  a new matcher surface bought for a cosmetic win. Which levels a model
  actually runs is the backend's per-model ruling, refused with a structured
  400 (`param:"reasoning.effort"`, `code:"invalid_value"`) before any
  generation — measured: gpt-5.5 runs `xhigh` and refuses `max`. Baking that
  ruling would be a copy free to go stale between bake and runtime, so the
  gateway clamps off the refusal itself (`translate.clamp_effort`): one rung
  down per retry, mirroring Claude Code's documented "highest supported level
  at or below" rule for its own models. A menu that over-offers costs one
  extra round-trip, never a dead turn.

  **It runs before `subagent-models`, and that ordering is load-bearing.**
  `enum` registers the ids in the very schema `discover_models` reads, so a
  subagent pinned to a Codex model is offered exactly when that model is really
  in the bundle. Registering *after* meant `subagent-models` had to be told
  about the ids out of band, so it landed a pin whether or not
  `codex-models` did: drop `codex-models` for a drifted anchor and the binary
  kept `model:"gpt-5.6-sol"` on an agent, pointing at a model nothing had
  registered, with the manifest asserting the override. Now the dropped patch
  takes its pins down with it.

  **Two resolvers, and why both.** `Ei` is the general one — it turns `opus`
  into `claude-opus-4-8`, its return value *replaces* the model before the
  request is built, and it passes an unknown-but-valid name straight through
  (`return e`). `J9n` runs only when managed `availableModels` are active and
  defaults to `null`, not passthrough. An id needs neither arm (identity
  *is* passthrough); a **family shortcut** (`sol` → the newest
  `gpt-<ver>-sol`) needs one in both, and `Ei`'s is what makes it work on the
  ordinary path.

  **The shortcut gate.** Shortcuts are registered only when the `Ei` anchor is
  present. Absent it, none are registered anywhere and the ids — which need none
  of this — carry on. Accepted-but-unresolved is the failure worth engineering
  against: it leaves the redirect (which matches ids only), reaches Anthropic as
  an unknown model, and 404s.

  **Why the "already added?" checks are bounded.** The resolver check matches
  only the contiguous arms *at the insertion point* (`_RESOLVER_ARMS`), never
  the whole bundle: a short word like `auto` occurs as `case"auto":return` in
  stock code, so a global check would skip its arm while the id arms still
  marked the step applied — a shortcut that resolves nowhere. The picker drops
  its build-time check for the same reason and leans on the runtime `.some()`
  guard it injects. `Ei`'s own idempotency is the `default:}` adjacency: it
  matches the pristine arm only.

  **Routing knows nothing about shortcuts.** The redirect tests `body.model`
  against the baked id array, and the context table is keyed the same way — by
  the time either runs, `Ei` has already rewritten the shortcut. Measured on the
  wire: `claude --model sol` arrives at the gateway as `"gpt-5.6-sol"`. That is
  why a drift in the shortcuts costs shortcuts and nothing else.

  **A diverted request still carries Claude Code's auth header.** Measured, not
  assumed: point a listener at the gateway port and a Codex turn arrives with
  `Authorization: Bearer sk-ant-oat01-…` and the whole prompt. The gateway
  ignores it and never forwards it, so the exposure is to whatever holds the
  port. Stripping it from `redirect` does **not** work — don't retry it blind.
  `options.headers` is the last source `buildHeaders` merges and its merge
  treats `null` as delete, so setting a null (or an inert value) there ought to
  win; neither reaches the wire. With the injected block proven to run — a
  marker spliced into the replacement URL came through in the path — a probe
  header set on the options object *and* on the local copy was absent from the
  request both times, so something between `buildRequest` and `fetch` discards
  `options.headers` on this build. A real fix needs its own anchor further
  down, in `prepareRequest` (it receives the final `Headers` and the URL): a new
  required step and new matcher surface, deliberately not taken for 0.2.0.

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
- **`org-label`** — replace or hide the welcome screen's third segment
  (`model · plan · <organizationName>`; for a personal claude.ai account the
  org name *is* the account email). Anchor: the one conditional template
  joining `.organizationName` behind `!process.env.IS_DEMO`, whose false
  branch is upstream's own no-org shape `` `${model} \xB7 ${plan}` ``. An
  empty label (`--org-label` with no value — empty is the *value* "hide", not
  "unset") emits that false branch verbatim, so the separator leaves with the
  segment; a label takes the org text's place, escaped for the template
  literal it lands in. Candidates count off the `\xB7 ${*.organizationName}`
  composition, so a reshaped ternary reads as `candidates > 0, applied == 0`
  — a matcher to repair — rather than the zero that would equally mean
  upstream retired the segment. Only this line is touched: the `/status`
  Organization/Email rows, the login screen, and the org's startup message
  (`"Message from <org>:"`) still show the real account.

## Removed patches

Kept here so nobody reintroduces them without knowing why they left:

- **`word-diff-bg`** — as of 2.1.216 the word spans are nested inside a row
  element that already carries the line background; the fallback could never
  change a pixel. Confirmed redundant in live A/B.
- **`installer-label`** — its target string left the bundle in ~2.1.186.
- **`redacted-thinking`** — untestable against the real API (no way to elicit
  a `redacted_thinking` block), and the native-only tool keeps its surface to
  what can be verified.
