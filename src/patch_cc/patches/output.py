"""Tool-call and diff rendering patches."""

from __future__ import annotations

from .. import js
from ..js import Edit, Source
from .base import GROUP_OUTPUT, Options, Outcome, Patch

# --------------------------------------------------------------- tool calls

_COLLAPSED = '"collapsed_read_search"'
_VERBOSE = "verbose"


def _arm(node: js.Node) -> js.Node | None:
    """The ``switch`` arm this label *labels*, rather than one it occurs in.

    The label is an authored name and the arm is a node; ``case"x":`` spelled as
    text is neither -- it is the two of them with the minifier's spacing in
    between, which is the one thing about a build that has no reason to hold.
    """
    arm = js.up(node, "switch_case")
    return arm if arm is not None and arm.child_by_field_name("value") == node else None


def _tool_call_verbose(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Render collapsed read/search rows as if verbose mode were on.

    The arm is the dispatch label; the row is whatever inside it is *handed* a
    ``verbose`` prop. Which render function receives it, whether the arm is an
    expression or a block, and what else it is handed are all the minifier's
    to spell -- a matcher per arm form, carrying neighbouring prop names to
    prove it has found the right call, dates itself to one build. The label
    proves that.

    Handed, not taken apart. A prop being passed is an object literal; the same
    name in a destructuring pattern is a local being bound, and writing ``!0``
    over the name it binds is not a value flip but rubble. The gate would catch
    it, which makes it this patch reported broken on a build that merely reads
    the prop somewhere else -- a shape upstream is free to ship, and one this
    rewrite has nothing to say about.
    """
    edits = []
    for node in source.find(_COLLAPSED):
        arm = _arm(node)
        if arm is None:
            continue
        # `scoped`: the row is what *this arm* hands a `verbose` prop, not what a
        # callback nested inside it hands one -- a decoy object in such a callback
        # would otherwise be flipped too.
        for obj in js.every(arm, js.of_type("object"), scoped=True):
            verbose = js.props(obj).get(_VERBOSE)
            if verbose is None:
                continue
            outcome.candidates += 1
            if not js.is_true(verbose):
                edits.append(Edit.replace(verbose, b"!0"))
                outcome.applied += 1
    return source.apply(edits)


# --------------------------------------------------------------- create diff

_CREATE = '"create"'
_UPDATE = '"update"'

#: What the created-file render is handed. Membership, never the whole set: the
#: arm draws several other things -- a "/plan to preview" placeholder, two
#: "Wrote N lines" summaries -- and these three pick it out of them alone, on
#: every build in the corpus. Demanding *exactly* these was one added prop from
#: reporting the anchor gone, which is precisely what upstream has already done
#: twice to the sibling render below.
_CREATE_PROPS = ("filePath", "content", "verbose")

#: What the updated-file render is handed. Membership for the same reason:
#: upstream keeps adding to this one (``previewHint`` and ``collapsed`` since
#: this patch was written), and none of that is a fact about which render it is.
_UPDATE_PROPS = ("filePath", "structuredPatch")

#: The word the arm's own summary pluralises the count with -- what tells the
#: line count from any other call the arm makes on the created file's text.
_LINE = '"line"'


def _sibling_arm(arm: js.Node, label: str) -> js.Node | None:
    """The arm for ``label`` in the same ``switch`` as ``arm``.

    Membership of one switch, never a direction: which of two arms upstream
    lists first is not a fact about either, and walking forward only read a
    build that lists `update` above `create` as the anchor being gone (0/0, on
    a switch plainly carrying both).
    """
    return next(
        (
            sibling
            for sibling in js.children(arm.parent)
            if sibling.type == "switch_case"
            and (value := sibling.child_by_field_name("value")) is not None
            and js.text(value) == label
        ),
        None,
    )


def _line_count(arm: js.Node, content: str) -> str | None:
    """How this build counts the lines of a created file.

    The arm already computes it for its own "Wrote N lines" summary, so the
    helper is borrowed rather than reimplemented, and what we render says the
    same number the arm beside it does.

    Which call that is, is settled by what the arm *says* about the number --
    it pluralises it as a ``"line"``, upstream's own word -- and not by being
    the first call in the arm taking the content. Any call does that: put a
    bare ``String(t);`` at the arm's head and it was baked in as the file's
    length, ``newLines:String(t)``, counted and green.

    There is deliberately no inline fallback. One stood here, never ran on any
    build in the corpus, and disagreed with the borrow it stood in for on the
    commonest input there is: upstream's helper drops the empty trailing field
    of a file that ends in a newline (and calls an empty file one line), while
    ``split`` counts both. A second answer nothing exercises is not resilience,
    it is a number that changes meaning on the build that finally reaches it.

    The arm counts the same file twice (once per summary it may draw), so what
    has to be single is the *answer*, not the call: two different counts would
    leave nothing to borrow.
    """
    borrowed = {
        js.text(counted)
        for summary in js.every(arm, js.of_type("call_expression"))
        if _LINE in [js.text(a) for a in js.arguments(summary)[1:]]
        for counted in [_counted(arm, js.arguments(summary)[0])]
        if counted is not None
        and counted.type == "call_expression"
        and [js.text(a) for a in js.arguments(counted)] == [content]
    }
    return borrowed.pop() if len(borrowed) == 1 else None


def _counted(arm: js.Node, number: js.Node) -> js.Node | None:
    """The expression behind the number a summary pluralises.

    A minifier is free to bind it to a local first and free not to, so both
    spellings answer one question -- and the local is resolved where it is
    *read*, because each branch of this arm declares its own.
    """
    if number.type != "identifier":
        return number
    declared = js.first(
        arm,
        lambda n: (
            n.type == "variable_declarator"
            and (bound := n.child_by_field_name("name")) is not None
            and js.text(bound) == js.text(number)
            and js.visible(n, number)
        ),
    )
    return declared.child_by_field_name("value") if declared is not None else None


def _create_diff_colors(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Render newly-created files through the diff component so lines get ``+``.

    A creation is a diff against nothing, so the row is re-pointed at the
    renderer the neighbouring ``update`` arm already uses, handed a patch whose
    every line is an addition. Both renders are found by what they are handed,
    so nothing here depends on which JSX runtime the build emits or on the
    order upstream lists the props in.
    """
    edits = []
    for node in source.find(_CREATE):
        create_arm = _arm(node)
        if create_arm is None:
            continue
        update_arm = _sibling_arm(create_arm, _UPDATE)
        if update_arm is None:
            continue

        # `js.only`, not `js.first`: the created render is rewritten and the
        # update render is read for the renderer/component/style it lends, and a
        # rewrite that takes the first of two matches is how a decoy wins without
        # a counter moving (docs/PLAYBOOK.md). Cardinality is 1 on every build in
        # the corpus, so this costs nothing today and turns a second match into
        # one broken patch naming its own confusion.
        created = js.only(
            js.every(
                create_arm,
                lambda n: (
                    n.type == "call_expression"
                    and all(name in js.call_props(n) for name in _CREATE_PROPS)
                ),
            ),
            "created-file renders in this arm",
        )
        updated = js.only(
            js.every(
                update_arm,
                lambda n: (
                    n.type == "call_expression"
                    and all(name in js.call_props(n) for name in _UPDATE_PROPS)
                ),
            ),
            "updated-file renders in this arm",
        )
        if created is None or updated is None:
            continue

        outcome.candidates += 1
        made = js.call_props(created)
        path, content, verbose = (
            js.text(made["filePath"]),
            js.text(made["content"]),
            js.text(made[_VERBOSE]),
        )
        renderer = js.text(updated.child_by_field_name("function"))
        component = js.text(js.arguments(updated)[0])
        style = js.text(js.call_props(updated)["style"])
        lines = _line_count(create_arm, content)
        if lines is None:
            continue

        edits.append(
            Edit.replace(
                created,
                f"{renderer}({component},{{"
                f"filePath:{path},structuredPatch:[{{"
                f"oldStart:1,oldLines:0,newStart:1,newLines:{lines},"
                f'lines:{content}===""?[]:{content}.split(`\\n`)'
                f'.map((__cc_line)=>"+"+__cc_line)}}],'
                f"firstLine:{content}.split(`\\n`)[0]??null,"
                f'fileContent:"",style:{style},verbose:{verbose},'
                f"previewHint:void 0}})",
            )
        )
        outcome.applied += 1

    return source.apply(edits)


PATCHES = [
    Patch(
        id="tool-calls",
        title="Detailed tool calls",
        group=GROUP_OUTPUT,
        fn=_tool_call_verbose,
        anchors=(_COLLAPSED,),
    ),
    Patch(
        id="create-diff",
        title="Colour new files as diffs",
        group=GROUP_OUTPUT,
        fn=_create_diff_colors,
        anchors=(_CREATE, _UPDATE, "structuredPatch:"),
    ),
]
