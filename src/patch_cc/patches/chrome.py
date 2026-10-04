"""UI chrome: spinner tips, the --version marker, and branding."""

from __future__ import annotations

from .. import js
from ..js import Edit, Source
from .base import (
    DEFAULT_BRAND,
    DEFAULT_SUFFIX,
    GROUP_CHROME,
    Options,
    Outcome,
    Patch,
    js_string,
    string_setting,
)

# ---------------------------------------------------------- spinner tips

#: The setting, which is the whole anchor. Every member *read* that decides
#: whether tips show reports disabled -- one rule, no paths to enumerate.
#:
#: The bundle gates tips in two shapes, a guard (`if(f().x===!1)return;`) and
#: an expression (`v.x!==!1`); a matcher per shape lets either drift while the
#: other carries the patch to green with tips still showing. A read that
#: answers "off" satisfies both by arithmetic, and satisfies whatever third path
#: upstream adds next without being told about it.
_SPINNER_TIPS = "spinnerTipsEnabled"

#: What tells the one read that is *not* a gate: `/config` shows the setting as
#: a row's `value` and hands the user's answer to its `onChange`.
_SHOWN, _HANDLER = "value", "onChange"


def _shown_by_a_control(read: js.Node) -> bool:
    """Is this read what a settings row displays, rather than a test tips obey?

    The distinction is the difference between reaching every path and reaching
    past them. A gate asks the setting what to do; a control asks it what to
    *say*, and answering "off" there does not disable anything -- it freezes
    the row at `false` and, because the row toggles by negating what it shows
    (`onChange(!value)`), makes pressing it write `true` into the user's
    `settings.json`. A patch that reaches out of the binary and into the config
    it was never asked to touch has left its blast radius, and `restore` cannot
    follow it there.

    Named by membership, so neither the row's other properties nor their order
    is described: the read is this row's shown ``value`` -- whether upstream
    spells that as ``value:read`` or ``get value(){return read}``, which is why
    the key is read through :func:`js.key_of` rather than off a ``pair`` alone --
    and the row carries the handler that writes it back. A bag with no enclosing
    object, or one that carries neither, answers no through the same expression
    rather than through a guard of its own.
    """
    row = js.entry(read)
    key = js.key_of(row)
    return (
        key is not None
        and js.text(key) == _SHOWN
        and js.carries(js.owner(row), _HANDLER)
    )


def _disable_spinner_tips(
    source: Source, _options: Options, outcome: Outcome
) -> Source:
    """Force spinner tips off by making every member read of the setting say off.

    One rule reaches every path *of that kind*, where a matcher per path lets
    either drift while the other carries the patch to green. The kind is the
    honest limit: a read is rewritten
    by putting `!1` where the read was, so it must be an expression. A
    destructured read (``let{spinnerTipsEnabled:x}=settings()``) binds a name
    instead, has no expression to replace, and would be missed -- and missed
    quietly, since the count of reads is the count of reads there are. What
    that costs is bounded by the anchor: no read rewritten is the patch
    failing, and the anchor `doctor` prints for a broken patch separates a
    renamed setting (anchor 0) from one that stopped being read as a member
    (anchor still there).
    """
    edits = []
    for node in source.find(_SPINNER_TIPS):
        read = js.up(node, "member_expression")
        if read is None or read.child_by_field_name("property") != node:
            continue
        if js.written(read) or _shown_by_a_control(read):
            continue
        outcome.candidates += 1
        edits.append(Edit.replace(read, b"!1"))
        outcome.applied += 1
    return source.apply(edits)


# ---------------------------------------------------------- version marker

_VERSION_TAIL = " (Claude Code)"


def _js_template_escape(text: str) -> str:
    """Make arbitrary text safe inside a JS template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _version_output(source: Source, options: Options, outcome: Outcome) -> Source:
    """Append a marker line to plain ``--version`` output.

    The marker text is the menu's marker field (default ``(patched)``), inserted
    just inside the version string after the product name. The site is found as
    the literal that *ends* in that name (:meth:`Source.literals`), not by the
    ``}.VERSION}`` run leading up to it -- an inlined member read closed between
    two substitutions, every character of it the minifier's to respell and one
    hoist (the kind 2.1.228 made of `IS_DEMO`) from moving. The authored tail
    selects the same two template literals on every build in the corpus and
    survives whatever upstream writes in front of it.
    The suffix is escaped for the template literal the tail sits in, and the
    insertion hangs off the fragment's own edge, so no offset is computed.
    """
    marker = (
        "\\n" + _js_template_escape(options.version_suffix or DEFAULT_SUFFIX)
    ).encode()
    edits = []
    for literal in source.literals(_VERSION_TAIL):
        fragment = js.first(
            literal,
            lambda n: (
                n.type == "string_fragment" and js.text(n).endswith(_VERSION_TAIL)
            ),
        )
        if fragment is None:
            continue
        outcome.candidates += 1
        # Whether the marker is already there, read from the literal the fragment
        # sits in rather than from a bundle-wide buffer -- the bundle is many
        # modules now, and this literal is one small node in one of them.
        literal_bytes = literal.text or b""
        after = fragment.end_byte - literal.start_byte
        if literal_bytes[after : after + len(marker)] != marker:
            edits.append(Edit.after(fragment, marker))
            outcome.applied += 1
    return source.apply(edits)


# ---------------------------------------------------------- branding

_BRAND = "Claude Code"


def _rebranded(node: js.Node, brand: str) -> bytes:
    """``node``'s text with the product name swapped for ``brand``.

    Re-emitted through :func:`js_string`, which is the only correct encoder for
    arbitrary text: hand-escaping the quote and the backslash once let a
    newline through into a double-quoted literal, producing a bundle that
    parsed nowhere and verified fine.
    """
    return js_string(js.text(node)[1:-1].replace(_BRAND, brand)).encode()


def _rendered(call: js.Node) -> list[js.Node]:
    """What a render call draws: its ``children`` prop, or its positional children.

    Upstream spells the same element three ways -- ``createElement(C,props,kid)``,
    ``jsx(C,{children:kid})``, ``jsxs(C,{children:[kid,...]})`` -- and has moved
    between them. A bag that *names* its children is the runtime that puts them
    there, and the arguments after it are that runtime's business; only a bag
    that names none leaves the content standing where `createElement` puts it.
    Which is not the same as reading both at once: the automatic runtime's third
    argument is the element's **key**, and counting it as a child stopped a
    keyed badge from being a badge. 2.1.232 already emits 448 three-argument
    `jsx`/`jsxs` calls, and none of them is a badge yet.
    """
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    for obj in (a for a in args.named_children if a.type == "object"):
        children = js.props(obj).get("children")
        if children is not None:
            return js.elements(children) if children.type == "array" else [children]
    return [a for a in args.named_children if a.type != "object"][1:]


def _is_branded(call: js.Node) -> bool:
    """Is this render the product's own name badge, rather than prose about it?

    ``bold:!0`` is what the startup and help banners have in common and what
    every other ``Claude Code`` in the bundle lacks -- and there are many: a
    notification title, a native-host id, a fallback name, and a sentence that
    begins with the words. Rendering the brand *in bold* is the badge; the
    prose is not a site this patch may touch, because renaming the product
    inside a system prompt changes what the model is told.
    """
    args = call.child_by_field_name("arguments")
    if args is None:
        return False
    return any(
        js.is_true(js.props(obj).get("bold"))
        for obj in args.named_children
        if obj.type == "object"
    )


def _is_name(literal: js.Node) -> bool:
    """Is this literal the product's *name*, rather than a sentence about it?

    The bundle renders plenty of bold prose that happens to contain the words
    -- "Claude Code successfully installed!", "No Claude Code sessions found" --
    and this patch renames the name badge, not everything that mentions the
    product. The badge is the whole literal (upstream pads one of them with
    spaces for its own layout); anything longer is a sentence.
    """
    return literal.type == "string" and js.text(literal)[1:-1].strip() == _BRAND


def _blank(node: js.Node) -> bool:
    """A string child that draws nothing -- upstream's own layout padding."""
    return node.type == "string" and not js.text(node)[1:-1].strip()


def _is_badge(call: js.Node, literal: js.Node) -> bool:
    """Is the product's name the *whole* of what this bold render draws?

    Boldness alone is not identity, and neither is "one of the children is
    exactly the name": upstream already splits prose into JSX children where
    the first child is the bare product name and the rest finish the sentence
    (``["Claude Code","'","ll be able to read..."]``, twice in the bundle). Bold
    one of those and a patch that asked only whether *some* child is the name
    would rename half a sentence -- and that sentence ends up in a system
    prompt, which is the one edit this patch must never make.

    So the whole render has to be the name: this literal among what it draws,
    and otherwise only upstream's own padding.
    """
    drawn = _rendered(call)
    rest = [child for child in drawn if child.start_byte != literal.start_byte]
    return _is_branded(call) and len(rest) < len(drawn) and all(map(_blank, rest))


def _styled_brand(call: js.Node) -> bool:
    """A call to the ``claude``-themed text helper: ``theme("claude",x)("...")``."""
    callee = call.child_by_field_name("function")
    if callee is None or callee.type != "call_expression":
        return False
    inner = callee.child_by_field_name("arguments")
    first = inner.named_children[0] if inner and inner.named_children else None
    return first is not None and first.type == "string" and js.text(first) == '"claude"'


def _branding(source: Source, options: Options, outcome: Outcome) -> Source:
    """Rename visible ``Claude Code`` branding to the user's chosen name.

    Only the visible startup/help chrome is touched, each site kind its own
    step so partial upstream drift is visible rather than averaged away.
    """
    if not options.rebrands:
        outcome.note("brand unchanged; nothing to do")
        return source

    brand = options.brand
    edits: list[Edit] = []

    def swap(step: Outcome, node: js.Node) -> None:
        step.candidates += 1
        replacement = _rebranded(node, brand)
        if replacement != node.text:
            edits.append(Edit.replace(node, replacement))
            step.applied += 1

    # The badge is the patch: it is the product's name where you always see it,
    # the startup banner and the help header, and a rename that misses it is a
    # rename nobody asked for -- with the manifest asserting the new name from the
    # manifest. Required, so a badge that stops being a bold render is loud
    # rather than a green tick over a binary that still says Claude Code. The
    # other two are sentences upstream may reword or drop, and their absence is
    # a build lacking a shape, not a dead feature.
    outcome.declare(required=("badge",), optional=("styled", "welcome"))
    badge = outcome.step("badge")
    styled = outcome.step("styled")
    welcome = outcome.step("welcome")

    # A name that is *inside* prose, so each literal is asked what it says
    # rather than each occurrence asked what it is part of -- and asked once,
    # however many times one sentence names the product.
    for literal in source.literals(_BRAND):
        # The startup/help name badge: the product name, drawn in bold.
        call = js.up(literal, "call_expression")
        if call is not None and _is_name(literal):
            if _is_badge(call, literal):
                swap(badge, literal)
            # The themed one-liner, whose helper is selected by the theme name.
            elif _styled_brand(call):
                swap(styled, literal)

        # The welcome line, which is a bare sentence with no chrome to identify.
        if literal.type == "string" and js.text(literal)[1:-1].startswith(
            f"Welcome to {_BRAND}"
        ):
            swap(welcome, literal)

    return source.apply(edits)


# ---------------------------------------------------------- startup org label

#: The welcome screen's info line: `model · plan` -- and, when the account has
#: an organizationName (for personal claude.ai accounts, that is the account
#: email), ` · <organizationName>` after it.
_ORG = "organizationName"

_IS_DEMO = "IS_DEMO"


def _interpolates_org(node: js.Node) -> bool:
    """Is this org-name read interpolated into a template literal?

    The composition -- the org name written *into a line* -- is the durable
    fingerprint, so candidates count off it rather than off the conditional. A
    reshaped conditional then reads as "found it, rewrote nothing" (a matcher to
    repair) rather than the zero that would equally mean upstream retired the
    segment, and the `/status` row ``{label:"Organization",value:r.organizationName}``
    is the same read outside any template and is not mistaken for one.

    Being *in* a substitution is the whole claim. Ending one is not: text that
    describes how the substitution closes reads ``${x.organizationName??""}``
    -- the same composition, one defensive operator later -- as the anchor
    being gone while the rewrite still lands, and the patch reports the state
    no row of the table has a meaning for (candidates 0, applied 1).
    """
    return js.up(node, "template_substitution") is not None


def _org_segments(source: Source) -> list[js.Node]:
    """Every composition of the org name into a line -- this patch's surface.

    One home for a question asked twice: the patch counts its candidates off
    this, and :func:`_org_absence` asks it to learn whether the build has a
    surface at all, so the two can never disagree about what counts.
    """
    return [node for node in source.find(_ORG) if _interpolates_org(node)]


def _org_absence(source: Source) -> str | None:
    """Why org-label has no work on this build, or ``None`` while it does.

    2.1.246 deleted the welcome-banner variant that composed the segment: the
    surviving banner draws `model · billing` with no org anywhere, which is the
    hidden state an empty org label asks for. That is upstream retiring
    the surface, not a matcher to repair, so it is said apart from broken --
    and asked of the bundle in hand, never of a version, so a build that draws
    the segment again offers the row with no code change. What this cannot
    tell apart is a composition respelled out of the locator's sight; the cost
    of that miss is a row missing from the menu on a build whose screen still
    shows the segment, and the corpus sweep is what keeps the locator honest.
    """
    if _org_segments(source):
        return None
    return (
        "the welcome screen composes no org segment -- upstream retired it, "
        "and hiding it is now upstream's own default"
    )


def _appended(line: js.Node, label: str) -> str | None:
    """What ``label`` adds to upstream's no-org line -- nothing, when hiding.

    A segment is introduced by the separator this very line already uses: the
    bytes between its last two interpolations, copied rather than spelled. What
    you copy cannot drift from what you copied it from, and spelling it put ours
    beside theirs the moment the two differed -- a build writing `/` where
    2.1.232 writes `\\xB7` baked ``${model} / ${plan} \\xB7 Ada``, half its
    punctuation and half ours.

    Read from the line's own bytes, not a bundle-wide buffer: the line is one
    template node in one module. A line with nothing to copy from (one
    interpolation, so no separator ever written) is a line this patch cannot
    extend, and says so by finding nothing rather than by inventing the
    character it would have needed.
    """
    if not label:
        return ""
    marks = [c for c in js.children(line) if c.type == "template_substitution"]
    if len(marks) < 2:
        return None
    line_bytes = line.text or b""
    separator = line_bytes[
        marks[-2].end_byte - line.start_byte : marks[-1].start_byte - line.start_byte
    ]
    return f"{separator.decode('utf8')}{_js_template_escape(label)}"


def _org_label(source: Source, options: Options, outcome: Outcome) -> Source:
    """Replace -- or, with an empty label, drop -- the welcome line's org segment.

    Upstream ships the no-org line as its own false branch, so hiding the
    segment is *selecting a string the bundle already carries*, separator and
    all, rather than composing one. A label is that same string with one more
    segment appended, introduced by the separator taken from the line itself
    (:func:`_appended`). Nothing here spells the separator, the model or the
    plan: the branch is reused verbatim and only extended, so a build that
    renames either interpolation, or writes `\\xB7` as a raw `·`, changes
    nothing.

    The demo test is recast rather than left in front. `&&` binds tighter than
    `?:`, so upstream's expression is ``(!demo && hasOrg) ? withOrg : noOrg``
    and its demo value is the *no-org string*, not `false`; replacing only the
    ternary would leave ``!demo&&`template` `` and make demo mode evaluate to
    `false`. A baked label is therefore ``!demo?line:noOrg``, and hiding emits
    the no-org branch alone -- both sides of the test want it.
    """
    label = options.org_label
    edits: list[Edit] = []
    seen: set[int] = set()

    outcome.candidates += len(_org_segments(source))
    for node in source.find(_ORG):
        line = js.up(node, "ternary_expression")
        if line is None or line.id in seen:
            continue
        with_org = line.child_by_field_name("consequence")
        no_org = line.child_by_field_name("alternative")
        condition = line.child_by_field_name("condition")
        if with_org is None or no_org is None or condition is None:
            continue
        if with_org.type != "template_string" or no_org.type != "template_string":
            continue
        if _ORG not in js.text(with_org) or _ORG in js.text(no_org):
            continue
        # The read, as the read it is. Asked as text ending in `.IS_DEMO` it was
        # a claim about the spelling of everything in front of the name -- the
        # very half upstream keeps changing here (`process.env` through 2.1.227,
        # a local from 2.1.228) and the one `js.reads` exists to stop describing.
        demo = js.first(condition, lambda n: js.reads(n, _IS_DEMO))
        tail = _appended(no_org, label)
        if demo is None or tail is None:
            continue
        seen.add(line.id)
        plain = js.text(no_org)
        edits.append(
            Edit.replace(
                line,
                f"!{js.text(demo)}?{plain[:-1]}{tail}`:{plain}" if tail else plain,
            )
        )
        outcome.applied += 1

    return source.apply(edits)


PATCHES = [
    Patch(
        id="spinner-tips",
        title="Disable spinner tips",
        group=GROUP_CHROME,
        fn=_disable_spinner_tips,
        default=False,
        anchors=(_SPINNER_TIPS,),
    ),
    Patch(
        id="version-marker",
        title="Mark --version as patched",
        group=GROUP_CHROME,
        fn=_version_output,
        anchors=(_VERSION_TAIL,),
        setting=string_setting("suffix", "version_suffix", DEFAULT_SUFFIX),
    ),
    Patch(
        id="branding",
        title="Custom startup name",
        group=GROUP_CHROME,
        fn=_branding,
        anchors=(f'"Welcome to {_BRAND}"', f'"{_BRAND}"'),
        setting=string_setting("brand", "brand", DEFAULT_BRAND),
    ),
    Patch(
        id="org-label",
        title="Startup org/email label",
        group=GROUP_CHROME,
        fn=_org_label,
        default=False,
        anchors=(_ORG, _IS_DEMO),
        setting=string_setting("org_label", "org_label", "", always=True),
        absence=_org_absence,
    ),
]
