"""Live (streaming) thinking, carried by the list the binary already draws live.

Claude Code streams tool-use blocks live. Its stream reducer keeps a list of
``{index, contentBlock}`` entries -- one opened when a ``tool_use`` block
starts, replaced as the block's input streams in, cleared when a message starts
or ends -- and the transcript wraps every entry into a virtual message and
draws it in order. That list is the *twin* of live thinking: same store, same
snapshot, same renderer, and a feature upstream ships working. So a thinking
block rides it. This patch opens an entry when a thinking block starts, grows
its text on every delta and drops it when the block closes, through the very
setter the reducer is handed for tool uses, and does nothing else: no state of
its own, no render path, no resets. The store, the subscription, the memo, the
id minting, the ordering and the clearing are all upstream's, and upstream
cannot break them without breaking its own live tool uses.

That is the lesson of this patch's history. Its render half -- a state
threaded into the conversation render, into the transcript renderer's
signature, into the memo that interleaves streaming blocks -- broke five times
between 2.1.235 and 2.1.257, each time on a spelling of React's data-access or
memoization fashion, and each repair bought exactly one build: every one of
those was a claim only this patch needed (the builds and the spellings are in
docs/PLAYBOOK.md). The reducer half, anchored on the API's own event strings,
has not moved since it became an insertion at dispatch points. Only that half
is left, plus the one setting default that makes the API stream summary text
at all -- which broke once itself (2.1.277), on the spelling of the ternary
that chose the request's display, and now rests on the setting upstream
chooses it from.

Two facts about upstream's list are relied on, both measured on the corpus.
The renderer keys a virtual message off its block's ``id`` when the block has
one and mints a fresh one per entry *object* otherwise, and the row it draws
is memoised as static for anything but a live tool use -- so the block carries
no id of ours, and a replaced entry, being a new object, is a fresh row drawn
with its full text (:func:`_block_start`). And the transcript re-renders when
an entry's ``contentBlock`` changes identity, which a replaced block does --
the same way the twin's own input accumulator replaces a tool-use entry as its
JSON streams in.
"""

from __future__ import annotations

from collections.abc import Callable

from .. import js
from ..js import Edit, Source
from .base import GROUP_OUTPUT, Options, Outcome, Patch

# --------------------------------------------------------- summaries setting

#: The setting upstream reads to decide whether a request asks the API for
#: summarised thinking text -- a name upstream wrote and publishes in its
#: settings schema, and the one fact every consequence is routed off: the
#: request's `display`, the beta header withheld while it is on, the `updates`
#: display the request otherwise falls back to. The request site those reach
#: is where upstream respells things; the name is what it keeps.
_SHOW_SUMMARIES = "showThinkingSummaries"


def _step_display_mode(source: Source, outcome: Outcome) -> Source:
    """Default the `showThinkingSummaries` setting on.

    Without a display mode in the request the API streams signature-only (or
    late) thinking, so the live row starves -- worst on short thinks. Upstream
    asks for ``"summarized"`` exactly when this setting is on, so the setting's
    read gains a default and the request is never touched: what the display
    value becomes, where it is chosen and how that choice is spelled are
    upstream's. 2.1.277 respelled all three at once -- the ternary that chose
    the value flipped its branches and grew a ``"highlights"`` mode inside the
    chosen side -- and the locator that stood here, reached through the env-var
    read sharing the value's ``let`` (comma-fusion, the minifier's) and asking
    for the consequence of a ternary whose alternative was ``void 0`` (the
    branch layout, upstream's), found nothing with every anchor standing.

    The read is one on every build in the corpus, spelled ``settings().name??!1``
    on all of them, and none of that is claimed: every member read is defaulted,
    as `spinner-tips` reads its setting, and a write is left alone for the
    reason it gives. An explicit ``false`` in the user's settings still wins,
    which a default written at the request could not honour.
    """
    step = outcome.step("display-mode")
    edits = []
    for node in source.find(_SHOW_SUMMARIES):
        read = node.parent
        if read is None or not js.reads(read, _SHOW_SUMMARIES) or js.written(read):
            continue
        step.candidates += 1
        step.applied += 1
        # Parenthesised, so it is a default *of that read* inside whatever
        # expression this build spells around it: a bare ``??`` may not stand
        # beside ``&&`` or ``||``.
        edits.append(Edit.replace(read, f"({js.text(read)}??!0)"))
    return source.apply(edits)


# ------------------------------------------------------------------ reducer

_CONTENT_BLOCK_START = "content_block_start"
_CONTENT_BLOCK_STOP = "content_block_stop"
_THINKING_DELTA = "thinking_delta"
#: The two block types that are thinking, as the API names them. A fused
#: ``case"thinking":case"redacted_thinking":`` chain is one dispatch point and
#: two separate arms are two; either way each is asked for by its label.
_THINKING_TYPES = ("thinking", "redacted_thinking")
#: The setter the reducer is handed for the live tool-use list -- upstream's
#: own property name, and the one name this patch rests on. It is the twin's
#: whole interface: whatever the reducer does to that list with it, this
#: patch does for thinking blocks with the same call.
_TOOL_USES = "onStreamingToolUses"
#: Our binding of that setter, taken out of the very pattern the reducer already
#: destructures its options through. A second pattern entry naming one property
#: is valid JavaScript and reads the same value, so the local upstream binds
#: it to is never read -- a minified spelling any nested block could shadow --
#: and "upstream already binds it" is not a case to handle.
_SETTER = "__cc_onStreamingToolUses"


def _block_start(event: str, setter: str) -> str:
    """Open a live entry for a thinking block, in the twin's own shape.

    ``{index, contentBlock}`` is what the tool-use arm pushes and what every
    consumer downstream reads: the stabiliser that dedupes entries against
    landed messages passes an entry straight through unless its id has landed,
    the renderer wraps ``contentBlock`` into a virtual message, and the
    interleaver appends the wrapped entries after the real messages in list
    order -- so a thinking block streamed before a tool call draws above it.
    An entry already open at this index is replaced in place, as the twin
    replaces one, rather than appended twice.

    The block is taken exactly as the API sent it, with no id of ours. The
    transcript's content renderer is memoised, and a row it holds no live
    tool-use id for counts as *static*: drawn once and never again, however
    the message behind it changes. An entry that opens with an empty block is
    therefore drawn empty, and a stable id would pin that empty row for the
    whole turn -- measured on 2.1.257, where the text arrived and the screen
    never moved. Without an id the renderer mints one per entry *object*, so
    every replaced entry is a fresh row drawn with its full text: upstream's
    own path for an id-less streamed block, and the reason a replacement per
    delta is the right update rather than a mutation.
    """
    return (
        f"{setter}?.((__cc_entries)=>{{"
        f"let __cc_index={event}.event.index,"
        f"__cc_entry={{index:__cc_index,contentBlock:{event}.event.content_block}},"
        f"__cc_at=__cc_entries.findIndex((__cc_seen)=>__cc_seen.index===__cc_index);"
        f"return __cc_at===-1?[...__cc_entries,__cc_entry]"
        f":__cc_entries.with(__cc_at,__cc_entry)}})"
    )


def _delta(event: str, setter: str) -> str:
    """Grow the live entry's text by one thinking delta.

    The entry is replaced with a fresh block carrying the appended text --
    exactly how the twin's input accumulator streams a tool call's JSON into
    its entry, and what makes the renderer mint the replacement a fresh row
    (:func:`_block_start`). A delta with no text -- the API also sends token
    estimates under this event -- and a delta for an index with no open entry
    both leave the list as it was, so nothing is published and nothing
    redraws.
    """
    return (
        f"{setter}?.((__cc_entries)=>{{"
        f"let __cc_text={event}.event.delta.thinking,"
        f"__cc_at=__cc_entries.findIndex((__cc_seen)=>__cc_seen.index==={event}.event.index);"
        f'if(typeof __cc_text!=="string"||__cc_text===""||__cc_at===-1)return __cc_entries;'
        f"let __cc_entry=__cc_entries[__cc_at];"
        f"return __cc_entries.with(__cc_at,{{...__cc_entry,contentBlock:"
        f'{{...__cc_entry.contentBlock,thinking:(__cc_entry.contentBlock.thinking??"")+__cc_text}}}})}})'
    )


def _stop(event: str, setter: str) -> str:
    """Drop the live entry when its thinking block closes.

    The block's real message lands in the same instant as its stop event, and
    the list is only cleared by upstream when the whole message ends -- for a
    tool use the stabiliser drops the entry the moment the landed message
    carries its id, but a thinking block carries none, so a live entry left
    standing would draw beside the landed block for as long as the answer
    streams. Dropped here, the live row hands over to the real one in one
    frame. Only a thinking entry at this index goes: a tool-use entry stops
    too, and upstream keeps that one until its message lands, so it is left
    exactly as the twin leaves it. With nothing to drop the list is returned
    as it was, so nothing is published.
    """
    types = "||".join(
        f'__cc_entry.contentBlock.type==="{kind}"' for kind in _THINKING_TYPES
    )
    return (
        f"{setter}?.((__cc_entries)=>{{"
        f"let __cc_kept=__cc_entries.filter((__cc_entry)=>"
        f"__cc_entry.index!=={event}.event.index||!({types}));"
        f"return __cc_kept.length===__cc_entries.length?__cc_entries:__cc_kept}})"
    )


def _reducer(source: Source) -> tuple[js.Node, js.Node] | None:
    """The stream reducer and the options bag it is handed the tool-use setter in.

    Identified by the dispatch points it *has* -- the same question its own arms
    answer and the one this patch goes on to edit -- and by being handed the
    setter this patch calls. Both halves are needed: *containing* a dispatch is
    not performing one (on 2.1.210 the engine loop and `submitMessage` each held
    the whole reducer somewhere inside them), so dispatching is asked of the
    scope itself, and the SDK's own stream classes switch on the same event
    strings without ever being handed a setter. The bag is also where the setter
    gets bound, so it belongs to the identity rather than to a second search
    afterwards, and both come back so the caller reads what was identified
    instead of reaching for it again. One reducer or none (:func:`js.only`): a
    build carrying two is a cardinality change to be told about.
    """
    found: dict[int, tuple[js.Node, js.Node]] = {}
    for node in source.find(f'"{_THINKING_DELTA}"'):
        handler = js.climb(node, lambda n: n.type in js.FUNCTIONS)
        if handler is None or handler.id in found:
            continue
        bag = _options_bag(handler)
        if bag is not None and all(
            js.first(handler, _routing(label), scoped=True) is not None
            for label in (_THINKING_DELTA, _CONTENT_BLOCK_START)
        ):
            found[handler.id] = (handler, bag)
    return js.only(list(found.values()), "stream reducers")


def _options_bag(handler: js.Node) -> js.Node | None:
    """The reducer's options bag, destructured from one of its parameters.

    Visible to the reducer's whole body, because that is what a binding every
    arm reads has to be: a pattern inside a nested function -- or inside a
    block of its own -- takes the same parameter apart under a name the arms
    cannot see. It is the pattern that binds the tool-use setter, since that is
    the property this patch adds a binding of.
    """
    block = js.body(handler)
    if block is None:
        return None
    taken = {js.text(js.binding(p)) for p in js.positional(handler)}
    for declarator in js.every(block, js.of_type("variable_declarator")):
        name = declarator.child_by_field_name("name")
        value = declarator.child_by_field_name("value")
        if name is None or name.type != "object_pattern" or value is None:
            continue
        if not js.visible(declarator, block):
            continue
        if js.text(value) in taken and _TOOL_USES in js.props(name):
            return name
    return None


def _dispatches(node: js.Node, label: str) -> bool:
    """Is this the test by which the reducer recognises one stream event?

    The dispatch string is the API's own vocabulary and does not churn. The read
    that reaches it is upstream's to spell -- ``e.type``, ``e.event.type``, and
    ``e.event?.type`` the day someone adds a defensive ``?`` -- so the literal
    is what is matched and the path between is never described. Spelling the
    whole test out is a claim on exactly that one character: the live block
    would never be marked finished and shimmer on after every turn, with the
    step reporting *absent* and the patch green.

    The operator is read, because the wrap runs the update on the side that
    dispatched: against a ``!==`` it would run on every event but this one.
    """
    if node.type != "binary_expression":
        return False
    operator = node.child_by_field_name("operator")
    if operator is None or js.text(operator) != "===":
        return False
    return any(
        side is not None and js.text(side) == f'"{label}"'
        for side in (
            node.child_by_field_name("left"),
            node.child_by_field_name("right"),
        )
    )


def _routing(label: str) -> Callable[[js.Node], bool]:
    """Predicate: this node routes that stream event.

    A factory rather than a closure spelled per call site, for the reason
    :func:`js.returns` is one: the question is asked of two events in one
    breath and of one event in another.
    """
    return lambda node: _routes(node, label)


def _routes(node: js.Node, label: str) -> bool:
    """Does this node route one stream event, whichever way this build routes it?

    Two spellings of one act: a ``switch`` arm labelled with the event, and the
    ``===`` test a build writes where it uses an ``if`` instead. Upstream uses
    both at once -- ``content_block_start`` is an arm and ``stream_request_start``
    a test, on every build we hold -- so which one carries a given event is not
    a fact about that event. :func:`_dispatch` inserts at the first and wraps the
    second; this is that choice asked as a question rather than performed as an
    edit, so a build that flips a spelling routes through the same code.
    """
    if node.type == "switch_case":
        value = node.child_by_field_name("value")
        return value is not None and js.text(value) == f'"{label}"'
    return _dispatches(node, label)


def _switch_on(case: js.Node, field: str) -> bool:
    """Does the ``switch`` holding this arm discriminate on ``.<field>.type``?

    The reducer switches over three stream unions -- the event type, the content
    block type, the delta type -- and a `case` label alone does not say which of
    them an arm belongs to: ``"thinking"`` is a content-block type, but a decoy
    arm of the same name in the *delta*-type switch drew the block-start update
    to itself, at 14/14 with `thinking-start` merely 1/1 -> 2/2. They are told
    apart by the authored name the discriminant reads ``.type`` off
    (``event`` / ``content_block`` / ``delta``), never by the minified receiver
    in front of it. Upstream folds side effects into the discriminant as a
    parenthesised comma sequence, whose *value* is its last element -- so
    unwrapping to the last child of each, once, reaches the read either way.
    """
    switch = js.up(case, "switch_statement")
    node = switch.child_by_field_name("value") if switch is not None else None
    while node is not None and node.type in (
        "parenthesized_expression",
        "sequence_expression",
    ):
        kids = js.children(node)
        node = kids[-1] if kids else None
    return (
        node is not None
        and js.reads(node, "type")
        and js.reads(js.receiver(node), field)
    )


def _dispatch(
    handler: js.Node,
    labels: tuple[str, ...],
    update: str,
    step: Outcome,
    *,
    on: str,
) -> list[Edit]:
    """Run ``update`` wherever the reducer dispatches one of these events -- in
    whichever spelling this build ships.

    A build routes a given event *either* as a ``case`` arm in a ``switch`` *or*
    as an ``===`` test written into an ``if``; upstream uses both across the
    reducer at once, and which one carries a given event is not a fact about that
    event. Binding each event to one of the two spellings would make a flip
    cost a repair -- on 2.1.233 the ``message_stop`` ``if`` sits one line above
    the ``switch`` it could fold into, and folding it would send a step to 0/0.
    Matching both spellings (:func:`_routes`) makes that flip cost nothing,
    which is the promise CONDUCT makes about a new spelling.

    The two spellings take the two edits they always did. A ``case`` arm gets the
    update inserted at its dispatch point, after the whole consecutive label
    chain (a fused ``case"a":case"b":`` arm once, split terminating arms once
    each), and only when its ``switch`` discriminates the ``on`` union -- so a
    decoy label in a sibling switch is not a dispatch point (:func:`_switch_on`).
    An ``===`` test is wrapped ``(test&&((update),!0))``, value-preserving so it
    survives any surrounding composition and needs no union check (the label
    *is* the union).
    """
    # Each dispatch point is an offset with the arm it belongs to, so the
    # insertion routes to the arm's own module (`Edit.at`'s `within`); the arm is
    # the natural anchor since the reducer, arm and update are all one module.
    points: dict[int, js.Node] = {}
    tests: list[js.Node] = []
    for node in js.every(handler, lambda n: any(_routes(n, label) for label in labels)):
        if node.type != "switch_case":
            tests.append(node)
        elif _switch_on(node, on):
            points[js.dispatch(node)] = node
    step.candidates += len(points) + len(tests)
    step.applied += len(points) + len(tests)
    return [Edit.at(at, f"{update};", within=points[at]) for at in sorted(points)] + [
        Edit.replace(test, f"({js.text(test)}&&(({update}),!0))") for test in tests
    ]


def _step_reducer(source: Source, outcome: Outcome) -> Source:
    """Put thinking blocks on the live tool-use list at the reducer's own arms.

    Four edits, all inside the one function: our binding of the setter at the
    front of the options pattern (valid whatever the pattern ends with -- a rest
    element or trailing comma there would make an append a SyntaxError no write
    verifier could see), the entry opened where the reducer dispatches a
    thinking block's start, the text appended where it dispatches a thinking
    delta, and the entry dropped where it dispatches the block's stop. Each
    dispatch point is its own required step, so a build that folds an arm away
    reads as that point's absence by name; and every step here answers for its
    own identity alone -- nothing is discovered in one step for another to
    depend on, which is how one moved shape would otherwise read as every step
    finding nothing.
    """
    step = outcome.step("reducer")
    found = _reducer(source)
    if found is None:
        return source
    handler, bag = found
    step.candidates += 1
    step.applied += 1

    event = js.text(js.binding(js.positional(handler)[0]))
    edits = [Edit.before(bag.named_children[0], f"{_TOOL_USES}:{_SETTER},")]
    edits += _dispatch(
        handler,
        _THINKING_TYPES,
        _block_start(event, _SETTER),
        outcome.step("thinking-start"),
        on="content_block",
    )
    edits += _dispatch(
        handler,
        (_THINKING_DELTA,),
        _delta(event, _SETTER),
        outcome.step("thinking-append"),
        on="delta",
    )
    edits += _dispatch(
        handler,
        (_CONTENT_BLOCK_STOP,),
        _stop(event, _SETTER),
        outcome.step("thinking-stop"),
        on="event",
    )
    return source.apply(edits)


# ------------------------------------------------------------------ assembly


def _live_thinking(source: Source, _options: Options, outcome: Outcome) -> Source:
    # Every step declared before anything runs: an expectation that only comes
    # into existence once its own rewrite succeeds can never report that
    # rewrite missing. All five are required -- without any one of them live
    # thinking is dead or draws twice, and there is no shape here some builds
    # merely lack.
    outcome.declare(
        required=(
            "display-mode",
            "reducer",
            "thinking-start",
            "thinking-append",
            "thinking-stop",
        )
    )
    source = _step_display_mode(source, outcome)
    return _step_reducer(source, outcome)


PATCHES = [
    Patch(
        id="live-thinking",
        title="Stream thinking live",
        group=GROUP_OUTPUT,
        fn=_live_thinking,
        anchors=(
            f"{_TOOL_USES}:",
            f'case"{_THINKING_DELTA}"',
            f'case"{_THINKING_TYPES[0]}"',
            f'"{_CONTENT_BLOCK_START}"',
            _SHOW_SUMMARIES,
        ),
    ),
]
