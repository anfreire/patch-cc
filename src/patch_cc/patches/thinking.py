"""Make thinking blocks visible in the normal UI instead of transcript-only.

Three gates hide a finished thinking block in the normal view, and all three
must go or the patch silently does nothing (which is exactly how it shipped
broken in 0.1.0):

1. **Grouping.** The activity-group builder swallows every thinking-first
   assistant message into the collapsed "✻ ... (thought for Ns)" group, so the
   message never reaches the renderer at all. This was the missed gate: the
   two rewrites below applied cleanly to a switch arm that thinking text never
   flows through.
2. **The null guard.** The ``case"thinking":`` arm returns null outside
   transcript/verbose mode.
3. **Presentation.** The renderer collapses the text to one italic line unless
   ``isTranscriptMode`` is set.

All three rewrites are therefore marked *required*: any one of them going
absent is a regression, not a build variant, and `doctor` reports it as such
instead of staying green on the other two.

Rendering is only half of it. A fourth gate is not in the UI at all: the
account's server-side experiment bucket, which decides whether the API puts any
text in the thinking blocks it returns. `thinking-summaries` is that half, and
without it the other three render an empty string perfectly.

`max-effort` lives here too: effort is thinking depth, so the module that owns
how thinking is shown also owns how hard it is asked for.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import js
from ..custom_models import EFFORT_LADDER
from ..js import Edit, Source
from .base import GROUP_MODELS, GROUP_OUTPUT, Options, Outcome, Patch

# ------------------------------------------------------------------ grouping

#: The property the grouping loop accumulates think-time into. It is what makes
#: one arm of that loop *the thinking arm*: of every branch deciding where an
#: entry goes, exactly one both accounts think-time and absorbs the entry into
#: the open group. Neither fact is a shape, so neither moved when 2.1.232
#: extracted the summary into two helper calls and split a comma-fused `if` --
#: a reshape anything modelling the arm body dies on.
_THINK_TIME = "thoughtForMs"

#: The group's summary line, written from the entry that is being absorbed.
_SUMMARY = "latestThinkingSummary"

#: How an entry joins a group: the list, and the call that adds to it.
_MESSAGES, _PUSH = "messages", "push"

#: Where the summary write is sent instead. The group object is built by one
#: factory and read by one reader, both by name, so an unread property is
#: inert -- and renaming the write is shape-free in a way deleting the
#: statement is not: upstream has spelled it as a comma-fused assignment inside
#: an `if` head *and* as a guarded statement of its own, and this edit is the
#: same single node either way.
_DROPPED = "__ccDroppedSummary"


def _absorbing(group: str, anchor: js.Node) -> Callable[[js.Node], bool]:
    """Predicate: this ``if`` accounts think-time *and* absorbs, in one branch.

    Bounded to the arm whose *consequence* holds the anchor. The climb starts at
    the think-time accounting and looks for the `if` that also absorbs into the
    same group; unbounded, a thinking arm whose absorb had drifted let it walk
    out to an *earlier* arm of the same `else` chain that happens to push into
    the same group local, and that arm's absorb was rewritten instead -- at 1/1,
    with the required step satisfied and thinking still swallowed. An arm the
    anchor sits in the `else` of has the anchor outside its consequence, so it no
    longer qualifies, and a genuinely drifted absorb now finds nothing (a loud,
    required-step failure) rather than the wrong node.
    """

    def want(node: js.Node) -> bool:
        if node.type != "if_statement":
            return False
        consequence = node.child_by_field_name("consequence")
        return (
            consequence is not None
            and consequence.start_byte <= anchor.start_byte < consequence.end_byte
            and _absorbed(consequence, group) is not None
        )

    return want


def _absorbed(arm: js.Node | None, group: str) -> js.Node | None:
    """This arm's ``<group>.messages.push(<entry>)`` -- into the group it accounts.

    Named by the object, not by the call: the group whose think-time this arm
    has just added to is the group it goes on to absorb the entry into, and
    those two facts about one local are what make this *the thinking arm*. A
    push into anything else is somebody else's -- ``.messages.push`` alone took
    the first one in the branch, so a decoy push ahead of the real one was
    rewritten instead of it, at `group-routing` 1/1 with thinking still
    swallowed.

    Of what the arm itself does: a push inside a callback declared here answers
    for the callback.

    Each hop is the read it is, never the chain spelled out: ``.messages`` and
    ``.push`` are fields of two member reads, and the group is a whole node
    compared against a whole node. Written as one string it also asserted the
    operators in between, so a defensive ``<group>.messages?.push`` -- one
    character upstream owes nobody an announcement about -- would read as this
    arm having no absorb at all, and `group-routing` is required.
    """
    return js.only(
        js.every(
            arm,
            lambda n: (
                n.type == "call_expression"
                and js.reads(fn := n.child_by_field_name("function"), _PUSH)
                and js.reads(list_ := js.receiver(fn), _MESSAGES)
                and js.text(js.receiver(list_)) == group
            ),
            scoped=True,
        ),
        f"ways of absorbing an entry into {group}",
    )


def _ungrouped(arm: js.Node) -> js.Node | None:
    """What this dispatch chain does with an entry that joins no group.

    The terminal ``else`` of the chain the arm belongs to -- flush the open
    group, push the entry into the visible list. Reusing it *verbatim* is what
    makes the rewrite independent of how this build spells either: the patch
    does not construct a flush call, it routes the thinking entry down the path
    the loop already has for entries that are not grouped.
    """
    tail = arm
    while (alternative := tail.child_by_field_name("alternative")) is not None:
        inner = alternative.named_children[0] if alternative.named_children else None
        if inner is not None and inner.type == "if_statement":
            tail = inner
            continue
        return alternative.named_children[-1] if alternative.named_children else None
    return None


def _reroute_grouping(source: Source, outcome: Outcome) -> Source:
    """Send finished thinking messages to the visible list, not the group."""
    step = outcome.step("group-routing")
    edits: list[Edit] = []
    seen: set[int] = set()

    for node in source.find(_THINK_TIME):
        accounted = js.up(node, "member_expression")
        if accounted is None or accounted.child_by_field_name("property") != node:
            continue
        # The open group, as this build spells it: what the arm adds think-time
        # to is what it absorbs the entry into.
        group = js.text(accounted.child_by_field_name("object"))
        arm = js.climb(node, _absorbing(group, node))
        if arm is None or arm.id in seen:
            continue
        absorb = _absorbed(arm.child_by_field_name("consequence"), group)
        ungrouped = _ungrouped(arm)
        if absorb is None or ungrouped is None:
            continue
        seen.add(arm.id)
        step.candidates += 1
        edits.append(Edit.replace(absorb, js.text(ungrouped).rstrip(";")))
        edits += [
            Edit.replace(name, _DROPPED)
            for name in js.every(
                arm,
                lambda n: n.type == "property_identifier" and js.text(n) == _SUMMARY,
            )
        ]
        step.applied += 1

    return source.apply(edits)


# ------------------------------------------------------------------ the arm

_THINKING_LABEL = '"thinking"'
_TRANSCRIPT = "isTranscriptMode"
_HIDE_IN_TRANSCRIPT = "hideInTranscript"


def _answers_null(branch: js.Node | None) -> js.Node | None:
    """The ``return null`` a branch answers with, if that is what it does.

    Both minifier statement forms (``if(x)return null;`` and the 2.1.216
    ``if(x){return null}``) are the same tree, so there is no longer a form to
    miss -- which is exactly how this patch silently broke once.

    The branch's *own* statements, never the subtree: an `if` nested inside this
    one is its own gate with its own answer, and reading it as this branch's
    made the two overlap -- one return in a batch twice, which is the corruption
    `Source.apply` refuses. Whatever else the branch does is upstream's and is
    left standing, because what is neutralised here is the answer, not the
    conditional around it: deleting the whole `if` took a statement upstream had
    put before the return with it, at 1/1 and green.
    """
    if branch is None:
        return None
    statements = js.children(branch) if branch.type == "statement_block" else [branch]
    return next(
        (
            node
            for node in statements
            if node.type == "return_statement"
            and [child.type for child in node.named_children] == ["null"]
        ),
        None,
    )


def _hidden(arm: js.Node, transcript: set[str]) -> list[js.Node]:
    """Every early return in this arm that hides the block outside transcript mode.

    Keyed on the flag the arm itself hands the renderer as ``isTranscriptMode``,
    so what is neutralised is the gate this patch is about and not any other
    guard the arm may grow: a defensive ``if(!param)return null`` reads exactly
    like this one to a matcher that asks only what the branch answers, and
    answering that one is a render this build never meant to draw.
    """
    found = []
    # `scoped`: an early return this arm makes, not one a callback nested inside
    # it makes -- a `return null` inside a handler answers for the handler, and
    # neutralising it is a render this arm never owned.
    for gate in js.every(arm, js.of_type("if_statement"), scoped=True):
        if gate.child_by_field_name("alternative") is not None:
            continue
        condition = gate.child_by_field_name("condition")
        if not any(
            js.text(node) in transcript
            for node in js.every(condition, js.of_type("identifier"))
        ):
            continue
        answer = _answers_null(gate.child_by_field_name("consequence"))
        if answer is not None:
            found.append(answer)
    return found


def _thinking_inline(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Route thinking past the collapse machinery and render it expanded.

    One rewrite in the activity-group builder (see :func:`_reroute_grouping`),
    then two inside every arm labelled ``"thinking"`` that renders with an
    ``isTranscriptMode`` prop: neutralise the early null-return, and force the
    renderer into transcript presentation (full markdown instead of the
    one-line collapsed form).

    The flag the arm hands its renderer is what ties the two together: it is
    the value forced to ``!0`` here and the name the early return is keyed on
    there, so neither rewrite is looking for a gate the other cannot see.
    """
    outcome.declare(required=("group-routing", "null-guard", "renderer-props"))
    guard = outcome.step("null-guard")
    props = outcome.step("renderer-props")
    source = _reroute_grouping(source, outcome)
    edits: list[Edit] = []

    for node in source.find(_THINKING_LABEL):
        arm = js.up(node, "switch_case")
        if arm is None or arm.child_by_field_name("value") != node:
            continue

        # Presentation is a set of properties, never a call shape: whichever
        # renderer this build reaches, and whatever else it is handed, the
        # bundle's own prop names are what say how a thinking block is drawn.
        # Of the object *passed* -- the same names in a destructuring pattern
        # bind locals, and forcing a value over one writes rubble rather than a
        # prop, which the gate would report as this patch broken.
        drawn = [
            obj
            for obj in js.every(arm, js.of_type("object"))
            if _TRANSCRIPT in js.props(obj)
        ]
        # What this arm calls transcript mode, in its own locals: the value it
        # hands over under that name. Every identifier inside it, for the reason
        # `subagent-prompt` reads through the expression rather than requiring a
        # bare one.
        transcript = {
            js.text(name)
            for obj in drawn
            for name in js.every(js.props(obj)[_TRANSCRIPT], js.of_type("identifier"))
        }
        for answer in _hidden(arm, transcript):
            guard.candidates += 1
            guard.applied += 1
            edits.append(Edit.replace(answer, b";"))

        for obj in drawn:
            forced = {_TRANSCRIPT: b"!0", _HIDE_IN_TRANSCRIPT: b"!1"}
            carried = js.props(obj)
            landed = False
            for name, wanted in forced.items():
                value = carried.get(name)
                if value is None or value.text == wanted:
                    continue
                edits.append(Edit.replace(value, wanted))
                landed = True
            props.candidates += 1
            props.applied += landed

    return source.apply(edits)


# ------------------------------------------------------- the experiment bucket

#: The bucket's own read. Claude Code caches a GrowthBook assignment and echoes
#: it to the API so the server applies the same bucket; a slot that withholds
#: thinking summaries is served thinking blocks carrying an empty string, which
#: every other patch here then renders faithfully.
#:
#: Every read yields nothing, and that is the whole rewrite -- there is no
#: getter to identify. Emptying *the* getter meant proving which function was
#: it, because replacing a brace-free body wherever the property appeared would
#: delete whatever an unrelated reader did; one `.atis` per bundle is today's
#: happenstance, not an invariant. A read that reports nothing is correct
#: however many there are, so the branch that had to tell them apart is gone.
#:
#: The property, not the optional chain in front of it: `?.` is the caller's
#: choice and `.atis` is the name. Spelled as text it also took the front of a
#: longer name -- ``Gx()?.atisVersion`` read as this bucket and became
#: ``(Gx(),void 0)``, discarding a value nothing here has any claim on.
_BUCKET_READ = "atis"

#: The header the bucket rides in -- the feature's own name, and durable in a
#: way any read site is not. Candidates are counted off *this* so a reshaped
#: read reads as "found it, rewrote nothing" instead of the zero that would be
#: indistinguishable from a build where the mechanism is gone entirely.
_BUCKET_HEADER = '"x-cc-atis"'


def _thinking_summaries(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Stop reporting the account's experiment bucket to the API."""
    outcome.candidates += source.count(_BUCKET_HEADER)
    edits: list[Edit] = []

    for node in source.find(_BUCKET_READ):
        read = js.up(node, "member_expression")
        if read is None or read.child_by_field_name("property") != node:
            continue
        # A write is a place, not a value: `(cache(),void 0)=x` is rubble, and
        # the gate would report it as this patch broken -- losing the read that
        # is the whole patch along with it. `spinner-tips` skips writes for the
        # same reason and out of the same home; upstream writes neither today,
        # which is exactly when a hazard is cheap to answer for.
        if js.written(read):
            continue
        cached = read.child_by_field_name("object")
        if cached is None:
            continue
        # The cache read is kept and its value discarded, so only the server's
        # view of the assignment changes: local feature values still come from
        # the on-disk cache, which is the promise this patch makes.
        edits.append(Edit.replace(read, f"({js.text(cached)},void 0)"))
        outcome.applied += 1

    return source.apply(edits)


# ------------------------------------------------------------- max effort
#
# `max` is a first-class level in session -- the level list the binary ships is
# ["low","medium","high","xhigh","max"], and the CLI and env var both accept it
# -- but persistence runs through two whitelists that stop at xhigh. Both are
# widened; either alone changes nothing (docs/PLAYBOOK.md), which is also why
# both steps are required.

#: The effort ladder from its one home, split at the rung this patch is about:
#: ``max`` is the top level (`_MAX`), and the binary's persistence whitelist
#: ships admitting everything below it (`_LEVELS`), which is how the whitelist is
#: told from the other functions that compare effort levels. Derived, so an
#: upstream level lands here without a second edit; the split assumes persistence
#: lags the session list by exactly the top rung, which it does today, and a
#: build that broke the assumption would report broken (loud), never silently
#: wrong.
_LEVELS = EFFORT_LADDER[:-1]
_MAX = EFFORT_LADDER[-1]
#: The persisted-settings schema entry. Its zod chain ends in `.catch(void 0)`,
#: so a clean binary reading a settings file that carries "max" (baked by us,
#: then reverted by a Claude update) treats it as unset rather than failing the
#: whole settings parse -- the property that makes this patch safe to lose.
_EFFORT_PROP = "effortLevel"


def _admits(condition: js.Node) -> set[str]:
    """The levels this condition tests against, as their values.

    The same membership question `schema` asks of the settings array, asked of an
    expression: what a guard admits is the set of levels it names, and a set is
    not a sequence here either. Asked as text -- ``'"low"' in js.text(condition)``
    -- it was a claim about quoting and spacing, made in the one module that
    exists to stop making claims about either, and it sat one build away from
    reading a single-quoted rewrite as the whole whitelist having vanished.

    Of the condition itself (``scoped=True``): a level named inside a callback
    written into the test is that callback's, not a level this guard admits.
    """
    return {
        js.text(level)[1:-1]
        for level in js.every(condition, js.of_type("string"), scoped=True)
    }


def _whitelist(node: js.Node) -> js.Node | None:
    """The *condition* admitting a level, reached from any level it names.

    Identity is what the guard *does*: it compares one value against every
    level and returns that same value. The gate is one tiny function and the
    whole choke point -- the `/effort` save path writes settings only when it
    returns a value, the startup resolver reads settings back through it, and
    the model picker persists through it too -- so write and read widen
    together in one edit.

    The condition, not the `if`, because the condition is what the rewrite
    extends and what the caller has to hold on to; handing back the `if` only
    to reach through it again for the same child is a second chance to reach
    for a different one.
    """
    guard = js.up(node, "if_statement")
    if guard is None:
        return None
    condition = guard.child_by_field_name("condition")
    consequence = guard.child_by_field_name("consequence")
    if condition is None or consequence is None:
        return None
    if not set(_LEVELS) <= _admits(condition):
        return None
    # The value admitted is the value returned -- which is what tells this
    # whitelist from any other function that merely compares effort levels.
    subject = js.first(condition, js.of_type("identifier"))
    if subject is None or js.first(consequence, js.returns(js.text(subject))) is None:
        return None
    return condition


def _max_effort(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Let ``/effort max`` save as the default for new sessions."""
    outcome.declare(required=("gate", "schema"))
    gate = outcome.step("gate")
    schema = outcome.step("schema")
    edits: list[Edit] = []
    seen: set[int] = set()

    for node in source.find(f'"{_LEVELS[-1]}"'):
        if node.type != "string":
            continue
        condition = _whitelist(node)
        if condition is None or condition.id in seen:
            continue
        seen.add(condition.id)
        gate.candidates += 1
        gate.applied += 1
        subject = js.first(condition, js.of_type("identifier"))
        # An upstream that has adopted the level itself is the goal already
        # achieved, not a miss -- the same judged-on-achievement rule
        # `subagent-models` follows. Asked of the same set the guard was
        # identified by, so "already admits max" and "admits every other level"
        # can never be answered two different ways.
        if _MAX in _admits(condition):
            continue
        edits.append(
            Edit.replace(
                condition, f'({js.text(condition)}||{js.text(subject)}==="{_MAX}")'
            )
        )

    for node in source.find(_EFFORT_PROP):
        if node.type != "property_identifier":
            continue
        pair = js.named(node)
        if pair is None:
            continue
        # The array is confirmed by what it *contains*, which is what tells it
        # from the twelve other `effortLevel` properties in the bundle -- and
        # from anything else composed inside the same value, since the
        # expression this build reaches its schema factory through is minifier
        # noise that is skipped rather than described, so "the first array in
        # there" is a guess about the noise. Never by the sequence it lists them
        # in, or by there being nothing else in it: a set is not a sequence here
        # either (`custom-models/validator` says so in as many words), so an
        # upstream reshuffle or one more level is a spelling of the same array,
        # and demanding the exact list made both read as the whole
        # persisted-settings schema having vanished.
        levels = js.first(
            pair.child_by_field_name("value"),
            lambda n: n.type == "array" and set(_LEVELS) <= set(js.strings(n)),
        )
        if levels is None:
            continue
        schema.candidates += 1
        schema.applied += 1
        if _MAX in js.strings(levels):
            continue
        edits.append(Edit.after(js.elements(levels)[-1], f',"{_MAX}"'))

    return source.apply(edits)


PATCHES = [
    Patch(
        id="thinking-summaries",
        title="Fix blank thinking blocks",
        group=GROUP_OUTPUT,
        fn=_thinking_summaries,
        anchors=(_BUCKET_HEADER, _BUCKET_READ),
    ),
    Patch(
        id="thinking-inline",
        title="Always show thinking",
        group=GROUP_OUTPUT,
        fn=_thinking_inline,
        anchors=(_THINKING_LABEL, _TRANSCRIPT, _SUMMARY, _THINK_TIME),
    ),
    Patch(
        id="max-effort",
        title="Persist max effort",
        group=GROUP_MODELS,
        fn=_max_effort,
        anchors=('"xhigh"', f"{_EFFORT_PROP}:", "Persisted effort level"),
    ),
]
