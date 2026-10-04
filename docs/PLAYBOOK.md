# Patch playbook

For maintainers. How the patches are built to survive Claude updates, and how to
repair one when a build breaks it.

## The rule

The minified bundle is regenerated on every Claude build, so identifiers churn
constantly. Two things survive that: the **names upstream's authors wrote**, and
the **grammar**. So there is one rule, and everything below is it applied:

> **Find by the name upstream wrote. Edit the grammar node. Never describe the
> syntax in between.**

The bundle is parsed (`src/patch_cc/js.py`), and the parse is what locates. A
patch scans for an authored name — a property, a `case` label, an API string, a
setting — takes the node that name *is*, climbs or descends to the node the edit
belongs to, and replaces that node:

```python
for node in source.find("latestThinkingSummary"):
    arm = js.climb(node, ...)          # the branch it identifies
    edits.append(Edit.replace(...))    # the node the edit belongs to
```

Everything a minifier regenerates lives strictly between those two: local names,
statement order, comma-fusion versus separate statements, braces around a single
statement, whether a helper was extracted, which JSX runtime is emitted. A tree
makes all of it invisible instead of obligatory.

The name is the **whole node**, never part of one — `agentType` is not
`subagentType` and `spinnerTipsEnabled` is not `spinnerTipsEnabledAt` — because
no later check can tell the difference: the longer name is a
`property_identifier` too, and a read of it rewritten as a read of this one
leaves `!1>0`, which counts, verifies and parses. Prose that *contains* a name
is the other question, and has its own search (`source.literals`): a product
name inside a sentence has no node of its own.

### The primitives

Each one answers a question the grammar settles, so no rule about the syntax
between two names has to be stated.

| primitive | the question it answers | instead of |
|---|---|---|
| `source.find(name)` | where is this authored name? | substring anchors — the name is the *whole* node, so `agentType` is not `subagentType` and `spinnerTipsEnabled` is not `spinnerTipsEnabledAt` |
| `source.literals(text)` | which strings say this? | a climb from a byte hit to the literal around it; prose is asked what it says, once per literal |
| `props(node)` | which properties does this object carry? | prop **order** and adjacency; `object` vs `object_pattern` (passed vs received) |
| `parameters(fn)` / `positional(fn)` | what does this function take? | destructuring-head matchers, and their declarator-edge bounds |
| `body(fn)` | what does this function do? | body matchers, which break when upstream grows a statement inside one |
| `reads(node, name)` | is this a member read of that name? | `.endswith(".x")` — a claim about the spelling of everything in front of the name, wrong in both directions at once |
| `written(node)` | is this read a *place* being assigned to? | the same hazard remembered per patch — every patch that rewrites *every* read of a name owes the answer |
| `first(node, want)` | which node under this one is the one I mean? | a skip over a schema factory whose callee is pure noise; the array under a property's value is *chosen by what it holds*, never by being the first one there |
| `first/every(…, scoped=True)` | what does this *scope* do, not counting the scopes inside it? | searches that read a nested function's work as the enclosing one's — a callback's `return null` taken for the arm's own, a function that merely *contains* the reducer's dispatches |
| `visible(declarator, site)` | can this site see that binding? | position ("the render falls after the `useState`") and scope-shaped guesses; nested functions **and** blocks, in one rule |
| `only(found, what)` | which one of these did upstream mean? | first-match-wins — for a rewrite there is one answer or none, and a second is a cardinality change to be told about |
| `dispatch(case)` | where does this `switch` arm begin? | label-chain scans, and modelling the arm body |
| `conjuncts(expr)` | is this an `&&`, and of what? | gate matchers spelled per site |
| `returns(name)` | does this answer with the value it was handed? | `return x` spelled per site, semicolon and all — the proof three matchers use to tell one small minified function from another |
| `Edit.replace/before/after` | which bytes are being changed? | off-by-one splices — a node has boundaries |

Three properties fall out rather than being enforced. **Boundaries are never
computed**: delimiting an object literal in minified JS means telling a regex
literal from division, and a hand-rolled scanner that guesses returns a `}` that
is merely wrong. **Scope is knowable**: a name spliced into a site has to be one that site can
*see*, and the grammar answers that (`visible`) where position and subtree
walks only guess. On 2.1.232 the two conversation renders sit in different
top-level components and only one declares the live-thinking state — a
bundle-wide answer threaded into both puts an out-of-scope identifier into the
other. It parses, so no gate can see it; it throws when that component renders.
Outwards is only half of lexical, and the other half is the block: a
declaration inside a nested function is invisible to the render outside it,
and so is one inside a plain block — `let` and `const` are the block's, `var`
the function's, and the grammar says which. Give that component a block-local
`useState` and an unscoped search would thread the binding into the render,
report 2/2, and silence the note that is the only sign a render was skipped.
The reducer's options bag asks the same question: the pattern every arm reads
has to be visible to the whole body, not one a nested function takes apart
under a name the arms cannot see.

**Cardinality is part of the invariant.** A name locates a set; a rewrite needs
one node. So the two are asked differently: a *registration* — an id into every
known-model array, an arm into every resolver of the kind, a prop into every
renderer with that signature — is a fact about a set and applies to all of them,
while a *rewrite* asks `only` for the single site and raises if this build
carries two. Taking the first match is how a decoy wins without a single counter
moving: a prepended `["sonnet","opus","haiku","opusplan"]` would absorb the
entire custom-model registration, and two throwaway functions with a
`case"best":` arm both resolvers — real sites untouched, every step green.

### What still takes judgement

The tree removes the *mechanical* fragility. It does not decide these:

- **Identity is still yours to prove.** A name locates; membership, or what a
  function does with its argument, is what says you found the right one. `model:`
  plus an array occurs 12 times; the Task tool's describe-string is what picks
  the one that matters. Weak identity is now the only way to match the wrong
  node.
- **Narrow, not broad.** `branding` renames the product's *name badge*, and the
  bundle is full of bold prose that merely contains the words — including
  sentences that would end up in a system prompt. Rewriting "every string that
  says Claude Code" would be one line shorter and wrong.
- **Rewrite at the dispatch point; never model the arm.** When an update is valid
  at arm entry, splice it there and say nothing about what the arm goes on to do.
  Arm bodies are upstream's busiest surface: 2.1.226 threaded one flag through
  five arms of the stream reducer in a single release. Entry insertion does
  reorder against mid-arm work, so it asks that the update be independent of the
  arm's own channels — decide that, don't assume it.
- **Reuse upstream's own expression where there is one.** `thinking-inline`
  routes a thinking entry down the loop's existing "not grouped" path by copying
  that branch verbatim rather than constructing a flush call; `org-label` builds
  its line from upstream's own no-org string, so nothing here spells the
  separator. What you copy cannot drift from what you copied it from.
- **Ride what upstream ships working** ([CONDUCT](CONDUCT.md)). Here it decides
  how many claims a patch makes: data put on a path upstream already runs owes
  the bundle one identity, the point where that path is fed, where a twin of
  the path owes one per step, each a claim only the patch needs.
  `live-thinking` is the worked case ([below](#live-thinking--streamingpy)).
- **Anchor on the fact, not on a consumer of it.** When upstream keeps a fact
  under a name of its own — a setting, a header, a store field — and every
  site that acts on it sits downstream of one read, default or flip *that
  read* and leave the sites alone: the sites are where upstream respells
  things, the name is what it keeps. 2.1.277 is the measure. It flipped the
  branches of the ternary that chooses the request's thinking display and grew
  a `"highlights"` mode inside the chosen value, while the fact that value is
  chosen from, `showThinkingSummaries`, stayed one member read in one spelling
  on every build in the corpus. A default written at the site — reached through
  the env-var read sharing the value's `let` (comma-fusion, the minifier's) and
  identified as the consequence of a ternary whose alternative is `void 0` (the
  branch layout, upstream's) — finds nothing there with every anchor standing;
  `display-mode` defaults the setting's read and sees no change. Where a value
  *must* be identified at its site, ask what it can *be* (`js.values`), never
  which branch it sits on.
- **Match an identity by the weakest claim that still proves it; hold a rewrite
  to the exact node.** The two jobs pull in opposite directions.
  `thinking-inline`'s null-guard neutralises only a `return` that answers
  exactly `null`, because deleting a conditional answer would delete behaviour
  — for a rewrite, doubt means stop. The custom-model resolvers' *identity* is the
  opposite case: 2.1.234 wraps the override resolver's rejection in a dead
  recognizer (`return f(e)?g(t):null`, the stub `f` answering `!1` — behaviour
  identical to `return null`), so "the default returns exactly `null`" reads
  that build's resolver as gone with every anchor count standing. Identity asks
  what the node *can* answer — membership among its possible values, the same
  rule props and arrays already follow — and exactness there is a break waiting
  on upstream's next harmless addition.
- **Never anchor on a minified local** (`A_`, `mET`, `wg6`). Structural: you
  capture the node, so there is never a reason to spell one.

### What "timeless" can mean

No matcher — regex, AST walk or dataflow pass — survives an upstream *semantic*
rewrite: a component that stops existing, a prop that becomes context, a resolver
folded into a table. Discovery from the bundle (below) *moves* the root invariant
and never abolishes one; agents still rest on `agentType`/`source`, models still
on the Task schema's describe-string. So the promise a matcher can keep is not
"never breaks":

> Insensitive to representation changes while the declared semantic invariants
> hold; fail closed and loudly when those invariants — or their cardinalities —
> change.

Which is also why `doctor` cannot certify grace. It measures whether *today's*
build matches, so a lucky repair reports exactly like a right one: a name list
grown by one entry, a new prop added to an ordered pattern, a path widened to
`(?:process\.env|<local>)\.IS_DEMO` — every one of those goes green, and every
one buys a branch against a single build. The counting below is the net for
drift; the rule above is the only thing standing between a repair and the next
repair.

## The many-module surface

Through 2.1.241 the bundle was one module — the entrypoint carried the whole app
— and every matcher searched that one buffer. 2.1.242 code-split it: the
entrypoint became a ~20 KB argv shim that lazily imports the app across well
over a thousand `chunk-*.js` modules ([INTERNALS](INTERNALS.md#the-21242-split-and-the-patchable-surface)).
The anchors did not move — they scattered. So `js.Source` **spans every
JavaScript module the container declares** (`Blob.js_modules`, discovered off the
entrypoint's own loader), and the one-module world is exactly the many-module
world with one module: `find`/`literals`/`count` sweep every module, `apply`
routes each edit to the module its node came from, and a patch says
`source.find(name)` / `source.apply(edits)` unchanged. This is representation
change of the crudest kind — the same code in more files — so it *moved* no
invariant and abolished none; the surface widened, the rules held. Three things
the move made explicit, each a rule the single-module world could leave unsaid
because the module was the bundle:

- **A node carries its module; an offset does not.** An `Edit` is built from a
  node, and the node's program root (`node.id`, distinct across parses) names the
  module the edit belongs to, so a batch spanning a dozen chunks routes each
  splice home and reparses only the modules it touched. It is why dedup keys are
  the node's `id`, never its `start_byte`: a minified offset repeats in a
  thousand modules, and two different sites at the same local offset are two
  sites, not one to skip.

- **A minified local is scoped to its module.** Resolving one — a hoisted
  `agentType:uu` constant read through its value — searches *its own* module
  (`Source.find_local`), never the bundle, where `uu` binds a string in a dozen
  unrelated chunks and `js.only` would rightly refuse to choose. Before the split
  the module *was* the bundle, so a bundle-wide `find` was a module-wide one; the
  two come apart only once the app is dealt across files. The rule this playbook
  states for identifiers within a build — "a name is a spelling until its scope
  is said" — reaches the module boundary too.

- **The gate reads code, not linkage.** tree-sitter's JavaScript grammar does not
  model a reserved word as an import/export alias (`export{x as if}`,
  `import{if as a}` — legal ES2015, which the split minifier emits when its
  two-letter alias generator lands on `if`/`in`/`do`), so it plants a localized
  `ERROR` inside the clause. patch-cc never locates in or edits module linkage —
  every anchor is executable code — so `Source.defect` reads *past* an error
  confined to an import/export statement and returns the first one that is not,
  while a splice that broke real code still lands outside linkage and is still
  caught. It is the gate's scope stated precisely, not a tolerance bolted on: the
  eight such modules on 2.1.243 parse clean for every purpose the tool has.

## Discovery instead of hardcoding

Anything the binary can enumerate for us, it does:

- **The entrypoint module** is the one the container *declares*
  (`entry_point_id`), never the one whose name we recognise — Bun resolves its
  own entrypoint by that same index, so patch-cc agrees with the runtime by
  construction rather than by coincidence. The name is upstream's to change
  (2.1.229 renamed it, `…/src/entrypoints/cli.js` → `…/cli`), and a list of
  known names would stop opening binaries the day after each rename — with the
  answer already parsed, already carried on the dataclass, and already written
  back unchanged by `rewrite`. A list that only ever grows is answering the
  wrong question. An id past the end of the module table is a corrupt blob and
  raises; there is deliberately no fallback to guessing by name, because a
  container that cannot say which module it runs is not one to write to.

- **Built-in agents** are the objects carrying `agentType`, `whenToUse` and
  `source:"built-in"` (`agents.discover_agents`) — membership of one object,
  so nothing is said about their order or about how much sits between them:
  an object has an end, where a scan window has to guess one. Definitions whose
  `whenToUse` begins with `"Internal` are plumbing
  and are not offered; an `agentType` that is a hoisted constant
  (`agentType:Ehr` with `Ehr="worker"` elsewhere — 4 of 11 built-ins on
  2.1.233) is read *through* its value, resolved to the single declarator that
  binds it, because a name read through its value is still one we can write.
  Only a type that resolves to no single string is skipped, as unreadable.
- **Model aliases** come from the Task tool's own input schema: the array
  reached from the `model` property whose describe-string starts
  `Optional model override` — `agents.model_enums`, one home because
  `custom-models` splices the chosen custom ids into the very arrays
  `discover_models` reads back out. Every such array, since the anchor is a
  sentence a second tool could carry too, and "every list of aliases a subagent
  may be pinned to" answers both questions where "the first one" answers
  neither.

A new upstream agent or model appears in the menu, in `doctor`, and in
`apply`'s pin validation without a code change. If the enum anchor ever vanishes,
discovery offers *nothing* rather than a guessed default: `doctor` prints
`models: inherit` alone, and a requested pin fails its required step instead of
landing on a hardcoded triple the real bundle may not even accept. A masked
absence is the one thing the whole report exists to prevent, so absence flows
through the path a present-but-different name does — the same emptiness
`custom-models`' own `enum` step already reports as broken.

**Names are checked against the binary.** A typed id or alias is refused when
the bundle in hand already answers to it (`claimed_model_names`, below), so a
newly shipped native model cannot be shadowed by a custom id or shortcut; the
only names refused without a bundle are the `claude-` prefix and the two words
the menu's pickers use (`inherit`, `keep`). Typed ids and optional aliases share
that validation, and selected ids and aliases must also be distinct.

The **collision** guard is derived, because registration cannot be trusted to
refuse one itself: `_register` counts a name the bundle already holds as
*landed* by design (an upstream that ships the id has achieved the goal), so
registering `opus` would report every step green and bake
`["opus"].includes(...)` into the redirect — diverting real Opus requests to the
endpoint. So a chosen id is refused *against the bundle's own tables* before it
is registered: the validator and enum strings and every registry `id` and
`provider_ids` value, all read from the binary in hand
(`patches.custom_models.claimed_model_names`). That catches the thirteen
provider ids on 2.1.233 (`us.anthropic.claude-opus-5` and kin) that are valid
slugs no `claude-` prefix would stop — a duplicate `provider_ids` is what the
catalogue builder rejects at first use with `provider id collision across
distinct entries`, bricking every command. `apply` and the menu refuse at the
front door (`validate_models`), the patch uses the same check before
`_custom_models` registers anything, and all of them read the same fact off the
bundle, so no path bakes a colliding id and no answer can fall behind upstream.

## The manifest

Every patched bundle ends with one comment line recording what was applied; its
shape lives in [INTERNALS.md](INTERNALS.md#the-manifest). What matters here is
that it makes the applied state a parse instead of a guess — several patches are value
flips (`verbose:!0`) that leave no other fingerprint — and that it is the
**only** evidence `is_patched` accepts. Authorship is declared, never inferred:
upstream ships `__cc_name`/`__cc_line`/`__cc_set` shell variables of its own
(2.1.227), so taking side effects of our edits as proof — the `__cc_`
identifier prefix, the `--version` marker — would read every pristine install
as patched and hard-block `apply`. A prefix tracks upstream's naming fashion,
never authorship (the same lesson as the `.enum(` callee above), and once
upstream ships it, it fires on every later build, so no narrowing rescues an
inferred fingerprint — `__cc_line` is both an arrow-param of ours and a shell
variable of theirs.

## How resilience is detected

`patch-cc doctor` runs every patch against a **clean** bundle (the pristine
backup if the installed binary is already patched) and reports, per patch:

- `candidates` — how many times the anchor/shape was found
- `applied` — how many rewrites actually changed something

Configurable patches are fed a synthetic configuration built from the bundle's
own discovered agents and models (every agent assigned a model different from
its current one, a test brand), so branding and the model overrides are
exercised for real — nothing is exempt from the dry run.

The patches are **composed**, one feeding the next, exactly as one pass of
`apply`'s fixpoint composes them. Running each against the pristine source in
isolation would be cheaper and could not see the one ordering this project calls
load-bearing — `custom-models` registering the ids `subagent-models` then pins —
nor anything at all about the bundle the run produces.

The composed result is then **baked and executed**: doctor writes it into a
temp binary through the same `container.write` as a real apply — staging,
round-trip verification, the container checks — and runs `<binary> --version`,
expecting the version-marker suffix in the output (our own edit's print, so the
check proves patched code *executes*, not merely that the binary boots around
it). Matching and running are different truths with different failure owners:
matchers break in the patches, the run breaks in the container layer, and
2.1.246 is the build where they split — the Bun 1.4.1 record chain and the
shared bytecode string table (docs/INTERNALS.md) live in bytes no module owns,
so a write that loses them has every matcher green, every module round-tripping
byte-perfect, and the written binary `SIGSEGV`ing in Bun's graph loader. The
temp binary is always removed; the backup under test is never written to.

Three readings of the two numbers, kept distinct:

| symptom | meaning |
|---|---|
| `candidates == 0` | the anchor is **gone** — a real regression |
| `candidates > 0, applied == 0` | shape found, rewrite was a no-op — usually already patched |
| `0 < applied < candidates` | some sites landed, some drifted — **partial**, so non-green on a clean bundle |

The middle row and the last are different verdicts: the first is `broken`
(nothing landed), the last `partial` (some did). A rewrite that *undercounts* —
`applied > candidates`, the durable witness behind more rewrites than the
witness has (a header read at several sites) — is not that row and stays `ok`.
Both `applied == 0` and the partial middle are the same concern one number
apart, so `Outcome.health` reads them off `applied < candidates` rather than off
`landed` alone, which would call `cand=2 applied=1` fully applied.

Both zero-rows are verdicts, not commentary: a rewrite has *landed* only when it
found something **and** changed something (`Outcome.landed`). Reading `applied`
alone is a hole exactly the size of the two patches that pay for a second,
durable witness — `thinking-summaries` counts off the header name behind the
read, `org-label` off the interpolation behind the conditional — so a renamed
`x-cc-atis` would leave every read rewritten, nothing left to rewrite them
*for*, and a green tick.

Where the two numbers come from one place, they move together and the second
row above cannot fire; it is worth knowing which patches those are, because for
them `candidates` is a restatement rather than a witness. The row earns its keep
exactly where a patch pays for a witness the rewrite does not produce: the two
above, and `custom-models`' registrations (a name already present is landed
without an edit). A witness that disagrees with its own rewrite reads as
`candidates == 0, applied == 1` — a state the table has no meaning for, and a
defect in the witness rather than in the build (see `org-label`).

`doctor` prints the documented anchor counts for any broken patch, so a `0`
next to an anchor points straight at what moved.

**A dry run is the one place the second row cannot mean "already patched."**
`doctor` is handed a *clean* bundle — the pristine backup when the install is
patched — so the benign reading of `candidates > 0, applied == 0` is
unavailable to it, and what is left is drift. It therefore exits non-zero on
`partial` as well as `broken` (`DryRun.unhealthy`), rather than printing `~` on
the patch's own line and closing with "all patches still match this build" and
a zero exit. `apply` reads the same verdict and answers differently on purpose
(`PatchReport.regressions` takes `broken` alone): there a partial patch *has*
landed and does ship, and dropping it would cost a working feature over a
missing refinement. Two questions, one `Outcome.health`, neither re-deriving
it.

### The syntax gate

Both numbers above are counts, and a count cannot tell a rewrite that landed
from a rewrite that landed *one prop-name to the left*. So the bundle is parsed,
and any `ERROR` or `MISSING` node **outside module linkage** aborts
(`src/patch_cc/js.py`; the linkage read-past is
[above](#the-many-module-surface)).

The parse is not a separate pass any more: locating already needs it, and an
edit costs one incremental reparse — ~75 ms on a 25 MB bundle against ~3 s for a
full one, measured to produce a structurally identical tree. So the gate sits at
**every batch of edits**, which is what lets it name the culprit: a patch whose
rewrite produces rubble raises out of `Source.apply`, is reported broken, and is
dropped from the set while the rest still apply. `apply` then checks once more on
exactly the bytes it is about to write — manifest included, after the fixpoint
settled — because interactions between patches are the one thing no single patch
can check.

A `Source` is **immutable**, and that is what makes the sentence above true on
every pass rather than only the first. `Tree.edit` mutates, so `apply` edits a
copy: editing the live one would leave the *input* holding a tree that no
longer describes its bytes, and since the fixpoint re-runs every patch from that
same pristine `Source`, one drifted matcher on pass 1 would have pass 2 report
every healthy patch as broken — each blamed for an anchor that was never
missing — and write nothing at all. `doctor` is the one shape that cannot see
it: a dry run is a single pass, and it hands each patch the previous patch's
*output*.

This is the only check that reads the bundle as a *language*, and it exists
because everything else agrees with rubble. `doctor` counts. The write verifier
re-extracts the bundle and compares it against what we meant to write, so a
corrupt splice matches itself perfectly. The damage is structural and easy to
produce: a callback's parameter read as the whole callback splices an arrow
function into its own argument list — which the gate catches at the batch that
made it, naming the step, with nothing written.

One corruption the parse *does* agree with is our own, so it is refused a step
earlier. Two edits over the same bytes splice each other's output — replace `x`
twice in `let x=1;` and the result is `let abcdefbcdef=1;`, which parses — so a
batch has to be **disjoint**: every edit inside the bytes no earlier one has
already rewritten. That is the same range check that keeps an edit inside the
bundle rather than a second rule beside it, and it costs nothing to hold; no
patch emits an overlapping batch on any build in the corpus. What it buys is the
matcher that *starts* to — an `if` and the `if` nested inside it, both read as
the same guard — being told so instead of writing the difference into a binary.

It is deliberately **not optional** — a safety check the install may or may not
have is not a guarantee, and this is the last thing standing in front of
CONDUCT's first rule. tree-sitter is what parses it, from a stable-ABI wheel,
with no Node or Bun involved.

What it is not is a compiler. tree-sitter recovers from errors and does not
implement every ECMAScript early-error rule, so a green gate means the token
stream still assembles into a tree — not that the program is legal in every
respect. That is the right scope: structural damage is what splicing causes. It
is also why an *out-of-scope identifier* is not its business — that one parses —
and why a splice that names a binding proves it visible itself (`js.visible`).

### Expectations — why a green tick means something

Counting alone cannot tell "this build lacks that shape" from "the feature is
dead": a patch whose optional shapes rewrite happily while a load-bearing one
is gone still changes bytes, and would read as green. So each sub-step is
declared with what its absence *means* (`Outcome.declare`):

| mark | meaning | absence is |
|---|---|---|
| `optional` | a shape only some builds carry | informational |
| `required` | the patch does nothing useful without it | a regression |

`Outcome.unmet()` turns a violation into a sentence (`required step
group-routing found nothing`); `Outcome.failures()` adds any exception the
patch raised, because a patch that threw and a patch that missed an
expectation are the same verdict wearing different clothes; `Outcome.health`
folds them into `ok` / `partial` / `broken`. Every surface (`apply`, `doctor`,
the menu) reads *those two* and never re-derives either — a verdict re-derived
from counts alone can print a red cross and "all patches still match" in the
same report. Adding a sub-step means deciding which row of the table it is in;
that decision is the whole safety net.

Four rules keep the net from having holes:

- **A step nobody declared cannot report its own death.** `branding`'s `badge`
  step is the product name where you always see it. Left undeclared, a badge
  whose boldness upstream makes conditional reads `0/0`, the patch reads green,
  the banner still says Claude Code, and the menu reports the new name from the
  manifest. So `badge` is required; `styled` and `welcome` are sentences
  upstream may reword, which is the other row of the table.

- **Count what the rewrite *achieved*, not that some rewrite happened.** A step
  built from many literal edits lands as soon as *one* of them applies, so an
  incidental edit can vouch for the essential ones: a reducer whose setter
  threading applies while every event arm has drifted reports hits and streams
  nothing. Every edit that *is* `live-thinking` is its own required step,
  counted at its own dispatch point, and there is no incidental edit left to
  vouch for it.
- **A step answers for its own identity, never for another step's discovery.**
  A step that waits on a name another step read out of a memo callback dies
  with that memo: 2.1.257 compiled the transcript renderer's memos into cache
  slots, and every step leaning on one would read *found nothing* — the report
  pointing away from the one shape that moved — while its own identity stood
  untouched. Nothing a step needs may come from another step having matched.
- **Declare an expectation before the work, not inside it.** A step created only
  by its own success cannot report its own absence. This is the API's shape,
  not a discipline: `Outcome.declare` is the only way a step comes to exist
  (up front, required and optional named apart), and `outcome.step(name)`
  retrieves — an undeclared name raises, so a typo cannot mint a silently
  optional step. Work that is owed conditionally declares under the same
  condition it runs (`custom_models._register_context`), and a name resolved from the
  bundle is declared the moment it resolves — `agents.bypassed_agents` has no
  step to name until the helper's guard gives up the pinned agent, and a guard
  that vanishes leaves the always-printed note, with no name left to declare.

`apply` acts on the verdict: a broken patch is re-run out of the final pass, so
its orphan edits never reach the binary, the manifest never claims it, and the
command exits non-zero. The healthy patches still apply.

That re-run is a **fixpoint**, not one retry. Patches see each other's output,
so dropping one can change what the next finds; the set is settled only when a
whole run comes back with nothing broken, and each patch is reported by the last
run it took part in. Judging the bytes of the final run by the verdicts of the
first is how a manifest starts lying. The manifest is held to the same rule:
`brand`, `suffix` and `models` are recorded only when *their* patch landed, so
the menu can never assert a name the bundle does not contain.

## Sub-steps, and why `live-thinking` has them

Most patches are one rewrite. `live-thinking` is five named sub-steps, because
a single hit count cannot tell "all landed" from "half silently drifted", and
each records its own `candidates`/`applied`:

- **`display-mode`** — the `showThinkingSummaries` setting defaults on, so the
  request asks the API for summarised thinking text; without it the stream
  carries signatures and no words.
- **`reducer`** — the stream reducer is found together with the options bag it
  is handed the live tool-use setter in, and our binding of that setter is
  added to the bag.
- **One step per dispatch point** — `thinking-start` (a thinking block opens an
  entry on the live tool-use list), `thinking-append` (a delta grows it) and
  `thinking-stop` (the block closes and the entry goes, as its real message
  lands). A build that folds an arm away reads as that point's absence *by
  name*, never as a bare count drop inside an aggregate. Each point is found by
  the API string it dispatches on and never by the read that reaches it:
  spelling a whole `===` test is a claim on one character — `e.event.type`
  gaining a `?.` would leave the live block never marked finished, shimmering
  after every turn, with the step reporting *absent* and the patch green.

All five are required. There is no optional step and no shape some builds
merely lack: the feature is those five edits or nothing, and every one is
asked of the bundle on its own — no step depends on a name another step
discovered ([Expectations](#expectations--why-a-green-tick-means-something)).

An *optional* sub-step that finds nothing is reported as absent, not broken —
it is just a shape this build doesn't have. A sub-step that finds a shape but
fails to rewrite it (`candidates > 0, applied == 0`) shows up in
`missed_steps()`.

The reducer is one step because there is one shape to find: a function that
dispatches on the thinking events and is handed the tool-use setter, its
options bag reached from the parameter it is destructured from rather than
matched. The setter is bound out of that bag under upstream's own property name
into one of ours, unconditionally: two patterns naming one property both read
it, so "upstream already binds it" is not a case to handle.

Notes print on every run, green ones included — an early warning held back
until something breaks arrives too late to be one. Absences are the noisy half
(most patches lack several shapes on any build) and wait for a verdict that is
not `ok`. Both surfaces draw the same list (`ui.findings`), so neither can lose
a note, or the exception that broke a patch, that the other shows.

## Repairing a broken patch

1. Get a clean bundle from a current binary:

   ```bash
   patch-cc extract ~/.local/share/claude/versions/<ver> > clean.js
   ```

   Since 2.1.242 that is every JavaScript module concatenated, each behind a
   `// ==== patch-cc module <n> ====` header, so `rg` still works over one file
   and the header says which chunk a hit lives in.

2. Run `patch-cc doctor`. Note which patch dropped to `candidates == 0`, or —
   for `live-thinking` — which sub-step.

3. Ask the bundle where the anchor went, as the name it is:

   ```bash
   rg -c 'case"collapsed_read_search"|thoughtForMs|spinnerTipsEnabled|Backgrounded agent' clean.js
   ```

   A count of `0` means upstream renamed or retired the thing. A count that
   stands while the patch fails means the *name is still there and the shape
   around it moved* — which is the case the tree is for, so the repair is
   usually a different climb, not a new anchor.

4. Reproduce the location in a REPL before editing any patch. `js.Source` is
   the whole tool, and the loop is short enough to run by hand:

   ```python
   from patch_cc import js
   src = js.Source(open("clean.js", "rb").read())
   for node in src.find("thoughtForMs"):
       arm = js.up(node, "if_statement")
       print(node.type, js.text(arm)[:120] if arm else "(in no if)")
   ```

   The `if arm else` is the lesson, not noise: a climb lands on nothing as
   readily as on a node — `thoughtForMs` is also a property of the group
   *factory*, which no `if` encloses — and `js.text` raises rather than hand
   back an empty name for a splice to be built from.

   Then update the climb in the relevant `src/patch_cc/patches/*.py`. Prefer a
   weaker structural claim (one more `props` membership, one fewer assumption
   about which node the edit hangs off) over a second branch; a second branch is
   the answer only when upstream genuinely ships two shapes.

5. Re-run `doctor` until the patch (and each expected sub-step) is green, then
   apply to a real binary and check the behaviour at runtime.

6. Sweep the fix over the versions you still have. `doctor` takes a path, and
   every binary patch-cc has ever touched left a pristine copy in
   `~/.local/share/patch-cc/backups/` — which is only the builds *this machine*
   patched, so `uv run scripts/corpus.py sync` first to make that the published
   set ([corpus.md](corpus.md)):

   ```bash
   sweep() { for b in ~/.local/share/patch-cc/backups/*.orig; do
     echo "== $(basename "$b")"; patch-cc doctor "$b" || true
   done; }
   git stash && sweep > /tmp/before.txt; git stash pop && sweep > /tmp/after.txt
   diff /tmp/before.txt /tmp/after.txt
   ```

   That is what keeps a repair honest, and the `diff` is the half that matters:
   a red build is loud on its own, but a *widened* locator shows up as an old
   build's counts quietly moving — 1 candidate becoming 2, a step that used to
   find nothing now finding something. Read the diff, not just the exit codes,
   and be able to say what each moved number means before you keep it. Nothing
   is persisted between runs and nothing needs to be: the corpus is on disk —
   enumerated with its hashes in [corpus.md](corpus.md) — so both sides are
   recomputed from the binaries themselves.

   Each build's report ends with its baked binary's own `--version` output (the
   smoke run above), so the sweep also proves every corpus build still *bakes
   and boots* — the half matcher counts cannot see, and the half 2.1.246 broke
   while every count held. A moved runtime line is as much a finding as a moved
   count.

## Patch reference

Grouped by source module. The menu's three groups (Output & display, Models &
effort, Chrome & branding) are display categories that each draw from several
modules — module says where a matcher lives, group says what the patch changes
for you. Each entry: what it changes, the stable anchor, and where it lives.

### Output & diffs — `output.py`

- **`tool-calls`** — force verbose collapsed read/search rows.
  Anchor: the `"collapsed_read_search"` label — the name, and the arm as the
  node it labels, never the two spelled together with the minifier's spacing in
  between. The row is whatever inside that arm is *handed* a `verbose` prop —
  an object literal, never a pattern binding the same name, because writing a
  value over a binding is rubble and not a flip. The label is what proves the
  right call has been found; which statement form the arm takes, and which
  neighbouring props the call carries, are the minifier's.
  *Value-flip* (`verbose:!0`) — the manifest is its only fingerprint.
- **`create-diff`** — render created files through the diff renderer with `+`
  lines. Anchors: the `"create"` and `"update"` labels of **one** `switch` —
  membership of that switch and never a direction, since which arm upstream
  lists first is not a fact about either, and walking forward would read a build
  that puts `update` above `create` as the anchor being gone. Both renders are
  found by what they are *handed*, by membership and never by the whole set:
  the created row carries `filePath`/`content`/`verbose`, the update row
  `filePath` and `structuredPatch` (plus `previewHint` and `collapsed` on
  newer builds — the same thing upstream may one day do to the other).
  Exact-set equality on the created row would read one added prop as the
  anchor being gone while `doctor` prints all three anchor strings present; on
  every build in the corpus the two rules count the same single site. The
  renderer, component and `style` are copied from the update arm rather than
  named, so the JSX runtime this build emits is never mentioned; the line count
  is borrowed from the arm's own "Wrote N lines" helper, which is required
  rather than fallen back on — a count of our own would disagree with it
  (upstream drops the empty field after a trailing newline; `split` keeps it).
  Which call *is* that helper is settled by the word the arm pluralises the
  number with (`"line"`, upstream's own), because "the first call in the arm
  taking the content" is a description of every call: it would bake a bare
  `String(t);` at the arm's head in as the file's length, `newLines:String(t)`,
  counted and green.

  **Alive by eye:** ask for a new file *via the Write tool* — the row reads
  `Added N lines` over green `+` rows. Two things legitimately show no diff and
  are not this patch failing: a file created through **Bash** (heredoc, `tee`,
  a script) is Bash output and never enters this renderer; and upstream keeps
  planting early returns *ahead of* the spliced render — plan-directory files
  ("/plan to preview"), condensed contexts (subagent progress rows, session
  summaries: "Wrote N lines to path"), and scratchpad/`.workshop.*` files in
  non-verbose re-renders ("Wrote N lines ⧉"). The live conversation renders
  the current message verbose, so the diff shows there today; those guards are
  the patch's effective coverage narrowing upstream-side, which `doctor` —
  counting matchers, not paths — cannot see. Measured on 2.1.234 (live,
  resume, plain/workshop/scratchpad files): every reachable path drew the
  diff.

### Thinking — `thinking.py`

- **`thinking-summaries`** — stop echoing the account's server-side experiment
  bucket, so the API returns thinking blocks with text in them.
  Anchor: the `atis` property, read as a member (header name: `x-cc-atis`) —
  the name, not `?.atis`, because the optional chain in front is the caller's
  choice and the text also fits the *front of a longer name*: `?.atisVersion`
  would read as this bucket and become `(<cache read>,void 0)`, discarding a
  value this patch has no claim on. **Every** read is
  replaced by `(<cache read>,void 0)` — the cache call kept, its value
  discarded — so there is no getter to identify. Emptying *the* getter would
  mean proving which function is it, because replacing a brace-free body
  wherever the property appears would delete whatever an unrelated reader does,
  and one `.atis` per bundle is happenstance rather than an invariant. A read
  that reports nothing is correct however many there are, so nothing has to
  tell them apart. Candidates are counted off the header name, not the
  reads, so a bucket that survives as something other than a member read reads
  as `candidates > 0, applied == 0` — something to repair — instead of the zero
  that would equally mean upstream retired the mechanism. The other half of
  that trade is the header *going*: rewriting reads that no longer feed it
  achieves nothing, which is why landing takes both counts. A read that is a
  *write* is skipped, out of the same home (`js.written`) `spinner-tips` draws
  from: upstream writes neither today, which is exactly when the hazard is
  cheap to answer for, and `(cache(),void 0)=x` is rubble that would take the
  one read this patch exists for down with it.
  Claude Code caches a GrowthBook assignment (`clientDataCacheSlots[...].atis`
  in `~/.claude.json`, one slot per account × entrypoint × model) and replays it
  to the API on every request so the server applies the same bucket. A slot in a
  bucket that withholds thinking summaries is served thinking blocks carrying a
  signature and an **empty string** — no `display` mode, effort level or
  `thinking.type` changes it, and two requests that differ only in the header
  differ in nothing else. Because the slot is per model too, one model can think
  visibly while another stays blank in the same session. Every other thinking
  patch then renders that empty string faithfully, so the symptom reads as
  "thinking works on one account and not another, same binary, same config".
  The value is read to set that one header, so reporting nothing lets the
  header's existing `if(value!==void 0)` guard skip it — nothing is sent, and no
  branch was added to stop it. Keeping the cache call is what preserves the
  next sentence's promise. Mind the breadth: this drops the
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
    and read widen together in one rewrite. It is identified by what it
    *does* — the value it admits is the value it returns — which is what keeps
    it off any other function that merely compares effort levels, and the
    rewrite extends the `if`'s condition rather than rebuilding the function,
    so neither minifier statement form is mentioned.
  - **`schema`** — the settings validator's `effortLevel` enum, reached from
    the property down to the array *that carries the four levels*, so the
    schema factory in between is skipped without being described and without
    "the first array in there" standing in for it. The array is
    confirmed by *containing* the four levels, which is what tells it from the
    twelve other `effortLevel` properties in the bundle — a set, not a
    sequence, the same rule `custom-models/validator` states outright. Asking
    for the exact ordered list would read both a reshuffle and a fifth level as
    the persisted-settings schema having vanished; membership counts the same
    one array on every build in the corpus. Its `.catch(void 0)` is the safety
    property: a **clean** binary reading a settings file that still says
    `"max"` (baked, then reverted by a Claude update) treats it as unset — the
    default effort, never a broken settings parse. Losing the patch costs the
    preference, nothing else.

  Both matchers tolerate the level already being present and count it as
  landed — an upstream that adopts `max` persistence itself is the goal
  achieved, not a miss (the same judged-on-achievement rule
  `subagent-models` follows). Effort semantics downstream are untouched: the
  org-limit clamp runs before the save, and a model that cannot run `max`
  clamps at request time exactly as an interactive `/effort max` does today.

- **`thinking-inline`** — make historical thinking blocks render inline.
  Three required rewrites:

  - **`group-routing`** — the activity-group builder swallows a finished
    thinking message into the collapsed "✻ … (thought for Ns)" group, so it
    never reaches the renderer. Anchor: `thoughtForMs`. Of every branch in that
    loop, exactly one both accounts think-time and absorbs the entry **into the
    same group** (`<group>.messages.push(…)`, where `<group>` is the local the
    think-time was just added to) — that pairing *is* the thinking arm, and
    neither half is a shape. Naming the group is what makes the second half an
    identity rather than a description: `.messages.push` alone matches any push
    at all, so a decoy one statement ahead of the real absorb would be rewritten
    instead of it, at 1/1 with thinking still swallowed. The absorb call is
    replaced by the chain's own
    terminal `else` copied verbatim (flush the group, push the entry into the
    visible list), and the summary write is sent to a dead property name rather
    than deleted, which is a single node whether upstream fuses it into an `if`
    head or guards it as a statement of its own. 2.1.232 extracted the summary
    into two helper calls and split the `if`: anything that models the arm body
    dies there.
  - **`null-guard`** — the early return that hides thinking outside transcript
    mode: an `if` keyed on the very flag the arm hands its renderer as
    `isTranscriptMode`, whose branch answers `return null`. Both minifier forms
    (`if(x)return null;` and `if(x){return null}`) are one tree, so there is no
    form to miss. What is neutralised is the **answer**, not the conditional
    around it: the `return null` becomes `;`, so a statement upstream puts
    before it still runs (deleting the branch would delete it, at 1/1 and
    green) and a branch that merely *contains* another gate's return cannot
    claim it — reading the subtree would put one return in a batch twice, which
    `Source.apply` refuses.
    Splitting the test across nested `if`s is a shape this does not follow: it
    reports the step absent, which is loud, rather than guessing which of the
    two conditions was the transcript one.
  - **`renderer-props`** — force `isTranscriptMode:!0` (and
    `hideInTranscript:!1` where present) on every object *passed* in the
    `case"thinking":` arm that carries them. Presentation is a set of
    properties, never a call shape — of a bag being handed over, never one
    being taken apart, for the reason `tool-calls` gives above.

  The component itself has no gate — an empty summary renders nothing, which is
  why trivially short thinks may still show no block.

### Live thinking — `streaming.py`

- **`live-thinking`** — stream thinking as it is generated, inline and in order
  with the tool calls around it. Five required steps
  ([Sub-steps](#sub-steps-and-why-live-thinking-has-them)) and one idea: a
  thinking block **rides the live tool-use list**.

  Claude Code already streams tool-use blocks live. The stream reducer keeps a
  list of `{index, contentBlock}` entries — opened when a `tool_use` block
  starts, replaced as its input streams in, cleared when a message starts or
  ends — and hands it to the store through the `onStreamingToolUses` callback
  in its options bag. Downstream, the transcript renderer wraps every entry
  into a virtual assistant message and draws it after the real ones, in list
  order. That list is the **twin** of live thinking: same store, same
  snapshot, same renderer, same memo, and a feature upstream ships working. So
  the patch puts thinking on it — opened, grown, and taken off again at the
  reducer's own arms — and does nothing else.

  - **`reducer`** finds the function that dispatches on `thinking_delta` and
    `content_block_start` (asked of the scope itself — *containing* a dispatch
    is not performing one; on 2.1.210 the engine loop and `submitMessage` each
    held the whole reducer inside them) **and** is handed `onStreamingToolUses`
    in a pattern destructured from one of its parameters and visible to its
    whole body. The SDK's own stream classes switch on the same strings and are
    handed no setter, which is what keeps them out. One reducer or none
    (`js.only`). The edit is one binding, `onStreamingToolUses:__cc_…,` at the
    front of that pattern: a second entry naming one property is valid and
    reads the same value, so upstream's own local is never read — a minified
    spelling any nested block could shadow — and "upstream already binds it"
    is not a case.
  - **`thinking-start`** inserts, at the `case"thinking":case"redacted_thinking":`
    dispatch point of the switch that discriminates on `.content_block.type`
    (`_switch_on` — the *delta* switch carries an arm of the same label, which
    would otherwise draw the update to itself), one call of that setter: open an entry
    `{index, contentBlock}` for this block, replacing one already open at the
    index rather than appending twice. The block goes on the list exactly as
    the API sent it, with **no id of ours**. The transcript's content renderer
    is memoised, and a row it holds no live tool-use id for counts as static —
    drawn once and never again, however the message behind it changes — so an
    entry that opens with an empty block is drawn empty, and a stable id pins
    that empty row for the whole turn (measured on 2.1.257: text arriving in
    the store, screen unmoved). Without an id the renderer mints one per entry
    *object*, so every replaced entry is a fresh row drawn with its full text:
    upstream's own path for an id-less streamed block.
  - **`thinking-append`** inserts, at `case"thinking_delta":` in the switch on
    `.delta.type`, one call that replaces the entry at this index with a block
    carrying the appended text — exactly how the twin's input accumulator
    streams a tool call's JSON into its entry, and what makes the renderer mint
    the replacement a fresh row. A delta without text (the API also sends
    token estimates under this event) or for an index with no open entry
    leaves the list as it was, so nothing is published and nothing redraws.
  - **`thinking-stop`** inserts, at `case"content_block_stop":` in the switch
    on `.event.type`, one call that drops the thinking entry at this index.
    The block's real message lands in the same instant as its stop event, and
    upstream clears the list only when the whole message ends: a tool-use
    entry is dropped by the stabiliser the moment the landed message carries
    its id, but a thinking block carries none, so a live entry left standing
    would draw beside the landed block for as long as the answer streams. A
    tool-use entry at the same index is left exactly as the twin leaves it,
    and a list with nothing to drop is returned as it was.
  - **`display-mode`** defaults the `showThinkingSummaries` setting on. Upstream
    asks the API for `"summarized"` exactly when that setting is on, and routes
    every consequence — the request's display, the beta header it withholds
    while summaries are on, the `updates` display the request otherwise falls
    back to — off one read of it, so that read
    gains a default (`(settings().showThinkingSummaries??!0)`) and the request
    is never touched. Every member read of the name, as `spinner-tips` reads
    its setting, and a write is skipped for the reason it gives; the read is
    one on every build in the corpus, spelled `settings().name??!1` on all of
    them, and none of that is claimed. An explicit `false` in the user's
    settings still wins. The request's own display value is not the place:
    2.1.277 flipped the branches of the ternary that chooses it (`!x?void 0:…`)
    and grew a `"highlights"` mode inside the chosen value, so a default
    written there — reached through the `CLAUDE_CODE_DISABLE_THINKING` read
    sharing its `let`, identified as the consequence of a ternary whose
    alternative is `void 0` — reads *found nothing* on that build with every
    anchor standing. Both claims are syntax between the anchors ([What still
    takes judgement](#what-still-takes-judgement)).

  **What is deliberately not done.** No state of ours: upstream's store carries
  a `streamingThinking` slot with a setter and a thirty-second linger, written
  with a finished summary when the assistant message lands and read back only
  to salvage a cancelled turn — no render reads it, and threading it into one
  would be a claim per React idiom upstream respells. No render path: nothing
  is threaded into the conversation render or the transcript renderer's
  signature, and no memo is rewritten. No resets beyond the block's own stop:
  upstream clears the list when a message starts or ends, and when the real
  message lands its thinking block renders in place through `thinking-inline`.
  No cross-module name: the setter is bound in the reducer's own pattern, so
  nothing discovered in one module is spelled in another.

  **What the twin guarantees, measured on the corpus.** The list's stabiliser
  drops an entry only when its id has already landed as a message or repeats
  another's; an entry of ours passes through. The transcript's content
  renderer is memoised and treats a row without a live tool-use id as static,
  drawn once — which is why a replaced entry has to become a fresh row (the
  minted id) rather than a changed one. The interleaver appends the wrapped
  entries after the real messages in list order, so a thinking block streamed
  before a tool call draws above it. The reducer's own tool-use arm replaces
  entries by index and its input accumulator replaces an entry's block per
  delta — the two idioms this patch copies. Two consumers read the list for
  something other than drawing it: the streaming-text preview hold, in its
  `"focus"` mode (the default is `"none"`), keeps the preview back while the
  list is non-empty, so there a live thinking entry holds it only while its
  block is open, which ends before the text after it streams; and the tool-use
  arm's size cap applies to tool blocks only, so thinking text is never
  truncated.

  **Alive by eye:** ask something that takes thought and watch the thinking
  block grow above the answer, then settle into the finished block when the
  message lands — no shimmer, no duplicate, no gap. It renders inline because
  `thinking-inline` routes thinking out of the collapsed activity group; with
  only this patch selected the live block is drawn where that group draws.
  Diagnose an empty live block from a transcript, not by eye — the column of
  lengths under `thinking-summaries` is the same check.

  **The claims this patch does not make.** A render path of its own would rest
  on React's data-access and memoisation fashions, which upstream respells
  build by build: a neighbour prop, a `useState` pair, a handing site, a
  snapshot destructure, a memo call — the React compiler turns those into cache
  slots (the conversation render from 2.1.247, the transcript renderer from
  2.1.257). Each such claim buys one build. The reducer's dispatch strings, the
  store's field name and the twin's own list are claims upstream cannot drop
  without paying for them, which is what CONDUCT's *ride what upstream ships
  working* asks.

### Subagents — `agents.py`

- **`subagent-prompt`** — show the Prompt block outside transcript mode.
  Every gate is the same conjunction — *in transcript mode, **and** there is a
  prompt* — and dropping the first half is the whole patch. Both halves are
  named by upstream: the transcript flag is a parameter property
  (`isTranscriptMode`), and the prompt is whatever the component passes as
  `prompt`. That pairing is what keeps the rewrite off the neighbouring
  `transcript && content && …` conjunction in the same component, which gates
  the agent's *output* and is not this patch's business.

  There are four gates on every build in the corpus — the block's mount, a
  second mount in the completed state, an early return that renders an empty
  state unless the prompt is showing, and the prompt render in the
  progress-messages component — and one rule reaches all four. Spelling them
  one by one is how the fourth goes unreached while its component's *empty
  state* is un-gated, so the block still never appears there.

  What counts as the prompt is every identifier *inside* the value passed under
  that name, not a value that is one. The progress-messages component has a
  single `prompt:h` pair feeding two of the four gates, so one defensive `??""`
  would take that component's Prompt block *and* its empty state with it, at
  `2/4` and green. Reading through the expression is strictly weaker and finds
  the same four gates on every build in the corpus.

  Neither half is asked to be on a particular *side* of the `&&`, and both are
  asked to be the variables they look like. Requiring the flag on the left
  would lose a whole Prompt block the moment one gate is spelled the other way
  round (`3/4`, green); and a name is only a spelling until you say which scope
  it belongs to — a callback inside the component taking its own `b` would
  match the component's `b`, and the conjunction rewritten would be the
  callback's own. Both rules find the same four gates on every build in the
  corpus.
- **`subagent-models`** — write the chosen model into each overridden built-in
  definition (discovered as above): rewrite the `model` literal when the
  definition has one, insert `model:"…",` before a property it must carry when
  it doesn't — a new property before an existing one is valid in any object
  literal, so nothing here looks for where the definition ends. Discovery runs
  once and every edit is committed together, so no offset can shift under
  another.
  Every requested override is a **required step**: each one reaching this patch
  has already been validated against the bundle by whichever surface asked for
  it, so a pin that cannot be written is not a shape this build lacks — it is
  the asked-for change failing, and the patch is dropped rather than shipping a
  manifest that claims it. An override whose target the definition already
  carries counts as landed: the step is judged on what it achieved, not on
  whether bytes moved.

  **The bypass:** one helper ignores the definition's model for a single pinned
  agent (Explore today) —
  `function f(def,main){if(def.agentType!==X.agentType||def.source!=="built-in")return def.model;…;return g(main)?PIN:"inherit"}`.
  Its **guard** identifies it and names the pinned agent (resolved by following
  `X` back to the definition assigned to it) — as a comparison of two nodes,
  never as the phrase `.source!=="built-in"`, which also asserts which side
  upstream writes the string on and would read `"built-in"!==e.source` as no
  helper at all. Following `X` back has one right answer or none, too: two
  definitions held under one minified name are two, and taking the first would
  map the bypass to the wrong agent, leave the real override inert, and fire no
  note because a bypass *was* found. Its **body** is what gets
  replaced, and upstream keeps growing it — 2.1.217 inserted a
  `CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP` escape hatch in the middle, which
  silently costs every Explore override to a matcher that spells the body. The
  body is a node, so there is nothing to skip. Resolving the agent from the
  guard alone is why a body reshape costs nothing at all: the body is replaced
  whole, however upstream grows it, and `bypass:<agent>` stands in the report
  as the record that an override was at stake.

  If the **guard** goes too there is no step to fail — nothing left names the
  agent — and no way to tell "upstream stopped pinning" from "the guard
  reshaped". That one is a note instead, on a patch that stays green; it is why
  notes print on green runs. Each helper is carried with the body node to
  replace, so identifying one helper and rewriting whichever one a fresh search
  finds first — how you neutralise an unrelated helper and report success — is
  not expressible, and a build that pins two agents gets both handled.

### Custom models — `custom_models.py`

`custom-models` registers external ids and diverts their Anthropic Messages
requests to a configured endpoint. Configuration, the key file and optional
discovery live in `src/patch_cc/custom_models.py`; the patch consumes a
`CustomModel` list and one endpoint URL, and makes no network request.

| step | what it changes | identity |
|---|---|---|
| `enum`* | ids and aliases in the subagent model enum | Task tool's model schema |
| `validator`* | known-model arrays | membership of the built-in aliases |
| `resolver`* | managed override resolver | built-in switch whose default can reject an id |
| `general-resolver`* | explicitly configured `/model` aliases | built-in switch with no rejection of its own |
| `redirect`* | replaces the request base URL | `buildRequest` that calls `buildURL`; options copy named by `defaultBaseURL` |
| `credential`* | replaces the original auth headers at the final SDK hook | `prepareRequest` with `url`/`options` parameters and an `_authState` read |
| `stateless`* | full history on every routed turn | passed objects carrying `model` and `modelHeldStateless` |
| `classic:mid_conv_system`* | disables stateful mid-conversation features for selected models | native model predicate querying this capability on its normalized argument |
| `classic:context_management`* | disables server context edits for selected models | same identity for `context_management` |
| `effort-limits`* | optional configured restrictions on effort capabilities | member read of `CLAUDE_CODE_MODEL_CAPABILITIES` |
| `picker` | native model picker rows | upstream's row assembler |
| `context`* | explicit per-model window | resolver that reads and returns `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
| `registry` | display names and effort capabilities | table carrying `models`/`aliases`, verified by entry fields |

* Required when owed: general resolution when an alias is configured;
stateless when the independent `thread_unsupported_request` witness exists;
effort restrictions when the chosen metadata excludes a native capability and
the build names `CLAUDE_CODE_MODEL_CAPABILITIES`; context when an explicit
window is configured.
The two classic predicates and credential isolation are always required. A
missing locator cannot remove its own requirement. A build without Message
Threads reports stateless absent, and a build without the capability override
reports effort-limits absent with a note: an advisory restriction never costs
the routing. A drifted locator on a build that has either is still a failure.

**Reuse the native per-model predicates.** The capability environment override
was added only in 2.1.267, while mid-conversation system messages predate it.
Declaring a classic-mode step required only when that read exists makes a dead
locator indistinguishable from an old build. Instead the two native predicates
return false for routed ids at entry, before any first-party fallback or force
flag. Their identity is the capability query's second argument, and its first
argument being a local model obtained by normalizing the predicate's own first
parameter. Registry entries and error recognizers are not that function. Each
predicate is located independently and held to `js.only`. Ordinary Claude ids
execute the original body unchanged. Shortcuts are included in membership tests
because planning can precede final request-model resolution.

**Credentials are edited after SDK preparation.** The hook is not inert: it may
add auth-state headers. Append the selected-model credential replacement to its
body after that work, and reject a hook with an early return. Normalize to
`Headers`, delete both `Authorization` and `x-api-key`, then set both from
`process.env.PATCH_CC_API_KEY`, or when that is unset from the key file, read
through `process.getBuiltinModule("fs")` (upstream's own idiom for builtins).
Only the file's path is baked, never the key. The same hook
covers streaming, retries, non-streaming requests and token counting. A missing
or ambiguous hook drops the whole patch, including its redirect.

**A base URL is more than an origin.** Preserve the built URL's API path after
removing the SDK base path, plus its query, and prepend the configured endpoint.
Trim trailing slashes from the base path before measuring it. This supports
prefixes on both bases without producing `/anthropic/anthropic/v1/messages` or
removing a character from `/v1/messages`. Node boundaries locate the splice;
no minified local or expression spelling is a matcher anchor.

**Metadata is a selection.** The menu reads `data[].id` from
`GET /v1/models?limit=1000` and, when an endpoint also answers
`?client_version=cpa` with `models[]`, joins that richer catalogue onto those
ids by exact match: display names, the default window (`context_window` before
the other known spellings), `max_context_window`, effort levels, and
`visibility: "hide"` to omit entries. No Anthropic headers are sent, because
some proxies cloak non-Claude ids for that client. Reported values are editable
suggestions; saved values survive a refresh, which only renews a model's window
choices. A default and a larger window become choices in the context picker,
never an automatic request for the maximum; the choices live in the cache and
only the chosen window is baked. Listed ids join the checklist unticked beside
the saved ones, and an id the endpoint does not list can be typed.

**An alias is a native shortcut.** It defaults to empty, is declared explicitly,
and resolves through Claude's existing resolvers to the unchanged endpoint id.
There is one model-registry entry and one picker row per model; an alias neither
renames that row nor becomes a second provider id. Context and effort lookups
accept both handles because native planning can happen before resolution. A
requested alias makes the general resolver required, so it cannot disappear
silently when that matcher drifts.

**Effort remains a native request setting.** Registry entries declare known
positive effort capabilities; the capability override disables `xhigh_effort`
and `max_effort` when excluded by configured levels, and all three when the
list is empty. Every custom model carries a list, and empty is the default:
there is no "unknown" state, because on the first-party path the gate's last
answer for an undeclared model is the provider fallback, which is *true* --
every level offered to a model that declared none, the one wrong *yes* this
patch can bake. The baked entries are prepended to the user's variable, so the user's
own entry for the same id wins: the parser keeps the last. A level outside the
ladder (`ultra`) is kept in the list and denies nothing. This does not change
the base low/medium/high menu or invent a tier the binary cannot express. The endpoint
owns translation and enforcement of individual provider levels. Registry
`capabilities` must always be an array: its reader is unguarded and the schema's
default is not applied to the raw catalogue. Do not register `advisor_rank`:
`/advisor` is a server-side feature and an external id is not an Anthropic advisor.

**Registration still precedes subagent overrides.** The enum populated here is
the one `subagent-models` reads, so a dropped registration takes its pins down
with it. Refuse collisions against the pristine bundle's own ids and provider
ids before registration; do not confuse a name already owned by Claude with an
id our registration can safely adopt. Strings are emitted through JSON and saved
models are validated and deduplicated at both configuration boundaries.

Verification: compare doctor counts across pristine backups and watch actual
turns through a recording endpoint. Check credential replacement (environment
and key file), full history on the second turn, absent stateful fields, effort
on the wire, path prefixes, namespaced ids, and ordinary Claude routing.
`--version` alone proves none of those behaviors. Exercise the real terminal
menu too.

### Chrome & branding — `chrome.py`

- **`spinner-tips`** — force spinner tips off. Anchor: the setting name. Every
  *member read* of `spinnerTipsEnabled` that a **gate** consults is replaced by
  `!1`, which satisfies the
  enabling guard (`f().x===!1` → true) and the enabling expression
  (`v.x!==!1` → false) by arithmetic, and satisfies whatever third path upstream
  adds next without being told about it. *Value-flip.* Candidates count off
  the setting name rather than off any one path, because a matcher per path
  lets either drift while the other carries the patch to green with tips still
  showing.

  One rule reaches every path *of that kind*, and the kind has three edges,
  each of them the difference between reaching every path and reaching past
  them:

  - The name is a **whole name**. `spinnerTipsEnabledAt` starts with it and is
    another setting; rewritten as this one it leaves `!1>0`, which counts,
    verifies and parses.
  - A read is an **expression**. An assignment *target* is a place, and `!1=!1`
    is rubble the gate reports as this patch broken, taking every other read
    with it — so a write is skipped. A destructured read
    (`let{spinnerTipsEnabled:x}=settings()`) binds a name, offers nothing to
    replace, and would be missed quietly; the count of reads is only ever the
    count of reads there are.
  - A **settings row is not a gate**. `/config` shows the setting as a row's
    `value` and hands the user's answer to its `onChange`, and answering "off"
    there disables nothing: it freezes the row at `false` and, because the row
    toggles by negating what it shows, makes pressing it write `true` into the
    user's `settings.json`. That one read is deliberately skipped — a patch that
    reaches out of the binary and into the config has left its blast radius, and
    `restore` cannot follow it there.

  What the whole rule costs is bounded by the anchor: no read rewritten is the
  patch failing, and the anchor `doctor`
  prints then separates a renamed setting (`0`) from one that stopped being read
  as a member (still there).
- **`version-marker`** — append `\n<suffix>` after `}.VERSION} (Claude Code)`
  (default `(patched)`; the menu's marker field names it; escaped for the
  template literal it lands in).
- **`branding`** — rename visible `Claude Code` startup/help strings to a
  chosen name. Three sub-steps, and the line between them and everything else
  is the point: `badge` is a render whose **whole** bold content is the product
  name, `styled` is the themed one-liner (`theme("claude",x)("Claude Code")`),
  `welcome` is the `Welcome to Claude Code…` sentence. The bundle holds sixteen
  exact `"Claude Code"` literals and many more inside prose — a notification
  title, a native-host id, a fallback name, sentences that end up in system
  prompts — and this patch renames the badge, not everything that mentions the
  product. A rule of "every string containing the words" would be shorter and
  wrong. Whichever JSX runtime the build emits is not mentioned: a props bag
  that *names* its children is the runtime that puts them there, and only a bag
  that names none leaves the content standing in the arguments after it. Which
  is not the same as reading both at once — the automatic runtime's third
  argument is the element's **key**, so counting it as a child would stop a
  keyed badge from being a badge, and 2.1.232 emits 448 three-argument
  `jsx`/`jsxs` calls (none of them a badge yet).
  *Whole* content, not "one of the children is the name": upstream already
  splits prose into JSX children whose first child is the bare product name
  (`["Claude Code","'","ll be able to read…"]`, twice), and bold is one prop
  away from either. Everything the badge draws is the name or upstream's own
  padding — which finds every badge on every build in the corpus: three sites
  through 2.1.216, four from 2.1.217 (upstream added one), and the count is a
  measurement rather than a promise.
  `badge` is required; see [Expectations](#expectations--why-a-green-tick-means-something).
  On by default, deriving `<username>'s Code`; the menu's name field
  names it explicitly.
- **`org-label`** — replace or hide the welcome screen's third segment
  (`model · plan · <organizationName>`; for a personal claude.ai account the
  org name *is* the account email). Anchor: the one conditional whose true branch
  interpolates `.organizationName` into a template and whose false branch does
  not — that false branch is upstream's own no-org line, and the replacement is
  **built from it**: taken verbatim to hide the segment, or with one more
  segment appended to bake a label. The separator that introduces that segment
  is *copied out of the line itself* — the bytes between its last two
  interpolations — so nothing here spells the separator, the model or the plan,
  and a build that renames either interpolation, writes `\xB7` as a raw `·`, or
  changes the separator outright, changes nothing. Spelling it would put ours
  beside theirs the moment the two differ: a build using `/` would bake
  `` `${model} / ${plan} \xB7 Ada` ``, half its punctuation and half ours.
  `IS_DEMO` is the node found inside the condition, never a path spelled out
  (`process.env.IS_DEMO` through 2.1.227, `J.IS_DEMO` on 2.1.228, `Q.IS_DEMO`
  on 2.1.229) — a spelled path dies at 2.1.228. The `!{read}&&` prefix is consumed,
  not left standing: `&&` binds tighter than `?:`, so upstream's expression is
  `` (!demo && hasOrg) ? withOrg : noOrg `` — which means its demo value is the
  *no-org string*, not `false`, and replacing only the ternary would leave
  `` !{read}&&`template` `` and make demo mode evaluate to `false`. A label is
  therefore `` !{read}?baked:no-org ``, measured to reproduce upstream's demo
  output exactly. An empty label (the menu's label field emptied — empty is the
  *value* "hide", not
  "unset") emits that false branch verbatim, so the separator leaves with the
  segment; a label takes the org text's place, escaped for the template
  literal it lands in. Candidates count off the org name **interpolated into a
  template** — the composition, found as the node it is rather than as text, so
  the `/status` row `{label:"Organization",value:r.organizationName}` is not
  mistaken for one. A reshaped conditional then reads as
  `candidates > 0, applied == 0` — something to repair — rather than the zero
  that would equally mean upstream retired the segment. Being *in* a
  substitution is the whole claim; ending one is a claim about how the
  substitution closes, and `${x.organizationName??""}` — the same composition,
  one defensive operator later — would then report the one state the table
  above has no row for: `candidates == 0, applied == 1`, the anchor "gone" and
  the rewrite landed. Only this line is
  touched: the `/status` Organization/Email rows, the login screen, and the
  org's startup message (`"Message from <org>:"`) still show the real account.

  **2.1.246 retired the surface.** Upstream deleted the welcome-banner variant
  that composed the segment; the surviving banner draws `model · billing` with
  no org anywhere — the hidden state an empty org label asks for, now
  upstream's own default. That is a semantic change, not a matcher to repair,
  and it is *absence*, reported apart from broken
  (CONDUCT): the patch declares its surface (`Patch.absence`, answered by the
  same `_org_segments` its candidates count off, so the two cannot disagree),
  and every surface derives its answer from the bundle in hand — the menu
  leaves the row out, `apply` skips a saved selection of it with the sentence
  as a warning, and `doctor` prints a `-` row apart from ✓/✗ and stays green.
  Nothing is keyed on a version, so a build that composes the segment again
  offers the row with no code change. What absence cannot tell apart is a
  composition respelled out of the locator's sight — that build would read as
  absent, the row missing from a menu whose binary still draws the segment —
  and the sweep over the corpus, where thirty-four builds carry the surface, is
  what keeps the locator honest.
