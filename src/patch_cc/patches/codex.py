"""Codex models: register the chosen OpenAI models, and route them to the gateway.

Two things happen here, and only when at least one model has been chosen:

* **Registration** makes Claude Code *accept and show* each model -- in the
  subagent ``model`` enum, the known-model validator, the ``/model`` picker, the
  model resolver, the context-window table, and the binary's own **model
  registry** (which names the model in the status line and banners, declares
  its effort capabilities, and makes it advisor-eligible). Alongside each id, a
  short **family shortcut** (``sol`` -> the newest chosen ``gpt-<ver>-sol``) is
  registered in the validator, *both* resolvers, and the picker, mirroring the
  binary's own ``opus`` -> ``claude-opus-4-8``: it resolves to the id before a
  request is built, so routing needs no separate knowledge of it (see
  :func:`patch_cc.codex.models.family_aliases`). The binary keeps two resolvers
  -- a general one every request uses and an override one for managed
  ``availableModels`` -- and a shortcut needs an arm in both; it is gated on the
  general one, so a drift there costs the shortcuts, never the bridge.

* **Routing** is the piece clodex does at the network layer with a TLS
  man-in-the-middle. patch-cc does it *in the bundle*: one injection in the
  Anthropic SDK's ``buildRequest`` swaps the request origin to the localhost
  gateway **only** when the body's model is one of the chosen ids. Every
  Anthropic-model request is left byte-identical -- same host, same plan token
  -- so nothing about the normal path changes, and if this step ever drifts,
  Claude models keep working and ``doctor`` flags the miss.

Everything this patch bakes comes from its ``Options``, exactly like the brand
and the subagent pins: the ids to register, and the port to route them to.
Nothing here reads a store of its own, which is what makes the binary's manifest
the one record of what a patched Claude Code answers to.

Required steps: without any one, the feature is dead --
``enum`` (subagent use), ``validator`` and ``resolver`` (the model is accepted
and resolves -- an id to itself, a shortcut to the newest id in its family),
``redirect`` (requests actually reach the gateway). ``general-resolver`` is
required too, but only exists when a shortcut is being registered (it is gated
in, above). ``picker`` (the ``/model`` list), ``context`` (the real window) and
``registry`` (the model table behind the status line, effort gating and
`/advisor`) are refinements: absent, you can still type ``/model <id>`` and get
a 200k default under the model's raw id.
"""

from __future__ import annotations

import json

from .. import js
from ..codex import DEFAULT_PORT, is_valid_port
from ..codex.models import CodexModel, family_aliases, family_of
from ..js import Edit, Source
from .agents import model_enums
from .base import GROUP_MODELS, Options, Outcome, Patch, Setting, js_string

# --- anchors: authored names and the grammar around them (docs/PLAYBOOK.md) ---

#: The known-model master list. Found by membership -- the built-in names it
#: holds -- never by their order. The regex this replaced spelled
#: ``["sonnet","opus","haiku","fable",...,"opusplan"]`` and was one upstream
#: reshuffle from death for it; the array on 2.1.232 already interleaves
#: ``"best"`` and three ``[1m]`` variants between those two ends.
_BUILT_IN_MODELS = ("sonnet", "opus", "haiku", "opusplan")

_BEST = '"best"'
_ALIASES = "aliases"
_MODELS = "models"
_REGISTRY_FIELDS = ("id", "family", "display_name")
_BUILD_REQUEST = "buildRequest"
_BUILD_URL = "buildURL"
_DEFAULT_BASE_URL = "defaultBaseURL"
_MAX_CONTEXT = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"

#: The JS regex literal that strips a URL's scheme+host, leaving path+query. A
#: raw string keeps the backslashes as the JS engine needs them.
_ORIGIN = r"/^https?:\/\/[^\/]+/"


def _append_strings(array: js.Node, names: list[str]) -> tuple[Edit, ...]:
    """Add the names an array does not already carry, after its last element."""
    present = set(js.strings(array))
    add = [name for name in names if name not in present]
    elements = js.elements(array)
    if not add or not elements:
        return ()
    joined = ",".join(js_string(name) for name in add)
    return (Edit.after(elements[-1], f",{joined}"),)


# ------------------------------------------------------------------- accept


def _register(
    source: Source, arrays: list[js.Node], names: list[str], step: Outcome
) -> Source:
    """Add these names to every array of the kind, and say so once per array.

    Registration is a fact about a *set* -- these names belong in every list of
    the kind this bundle keeps -- so it neither has to know which list upstream
    meant nor can be starved by a second one. A decoy carrying the four built-in
    model names absorbed the whole registration when this took the first match:
    real list untouched, ids nowhere, eight of eight steps green.

    A name already there counts as landed, for the reason `max-effort` and
    `subagent-models` say: the step is judged on what it achieved, and an
    upstream that ships the name itself has achieved it.
    """
    edits: list[Edit] = []
    for array in arrays:
        step.candidates += 1
        added = _append_strings(array, names)
        step.applied += bool(added) or set(names) <= set(js.strings(array))
        edits += added
    return source.apply(edits)


def _validators(source: Source) -> list[js.Node]:
    """Every known-model array -- the list that gates *resolution*, not just use."""
    found = []
    for node in source.find(f'"{_BUILT_IN_MODELS[-1]}"'):
        array = js.up(node, "array")
        if array is not None and set(_BUILT_IN_MODELS) <= set(js.strings(array)):
            found.append(array)
    return found


def claimed_model_names(source: Source) -> set[str]:
    """Every name the bundle's own model machinery already answers to.

    Registering a chosen Codex id that collides with one of these is what bricks
    the binary or hijacks a real model, so it is what the id must be refused
    against -- *derived* from the bundle in hand, never a hardcoded snapshot that
    upstream can outgrow (`codex.models._RESERVED`, docs/PLAYBOOK.md):

    * a duplicate ``provider_ids`` value makes the registry's own ``safeParse``
      throw ``provider id collision across distinct entries`` at first use, so
      every command dies -- and 13 of these on 2.1.233 (``us.anthropic.claude-
      opus-5`` and kin) are valid slugs that the ``claude-`` prefix guard never
      catches;
    * a name a validator or resolver already owns (``opus``) registers as a
      no-op the step counts as landed, then bakes ``["opus"].includes(...)`` into
      the redirect and diverts that model's own requests to the gateway.

    Both live in tables this module already reads (`_validators`, `model_enums`,
    `_model_table`), so the guard is the same fact the registration uses, read
    once more to refuse rather than to write.
    """
    names: set[str] = set()
    for array in (*_validators(source), *model_enums(source)):
        names |= set(js.strings(array))
    table = _model_table(source)
    for entry in js.elements(table) if table is not None else []:
        fields = js.props(entry)
        identity = fields.get("id")
        if identity is not None and identity.type == "string":
            names.add(js.text(identity)[1:-1])
        providers = fields.get("provider_ids")
        if providers is not None and providers.type == "object":
            names |= {
                js.text(value)[1:-1]
                for value in js.props(providers).values()
                if value.type == "string"
            }
    return names


# ----------------------------------------------------------------- resolvers
#
# The binary has TWO model resolvers and a shortcut needs an arm in both. They
# are told apart by what each *answers* for an unknown model, never by their
# minified names or by a statement form: the override resolver (reached only
# when managed `availableModels` are active) has a closed list and rejects --
# null is among what its default can answer -- while the general resolver (the
# one every ordinary request uses, which turns `opus` into `claude-opus-4-8`)
# has no answer of its own and falls through to passing the name straight back.
# Both identities are asked positively, of every model resolver, and an arm
# answering neither raises instead of swelling the other side: a complement
# cannot report its own absence.


def _best_arms(source: Source) -> list[js.Node]:
    """Every model resolver's ``"best"`` arm -- where new model arms are spliced.

    The label is the name and the arm is the node; ``case"best":`` written out
    is the two of them with the minifier's spacing in between. The switch also
    has to *be* a model resolver -- its labels carry the built-in models, the
    same membership `_validators` asks of its array -- because everything here
    applies to every arm of the kind: a throwaway ``case"best":`` in unrelated
    code must be nothing to a registration that would splice into it and to a
    classifier that would be asked what it is (`_resolvers`).
    """
    found = []
    for node in source.find(_BEST):
        arm = js.up(node, "switch_case")
        if (
            arm is not None
            and arm.child_by_field_name("value") == node
            and set(_BUILT_IN_MODELS) <= _labels(arm)
        ):
            found.append(arm)
    return found


def _labels(arm: js.Node) -> set[str]:
    """The string labels of this arm's whole ``switch``, as their values.

    A set: which names the switch dispatches on is the question, and neither
    their order nor what sits between them is part of it.
    """
    return {
        js.text(value)[1:-1]
        for case in js.children(arm.parent)
        if case.type == "switch_case"
        for value in [case.child_by_field_name("value")]
        if value is not None and value.type == "string"
    }


def _answer(statement: js.Node) -> js.Node | None:
    """The expression a ``return`` answers with -- nothing for a bare ``return;``."""
    parts = [child for child in js.children(statement) if child.type != "comment"]
    return parts[0] if parts else None


def _may_answer_null(expr: js.Node | None) -> bool:
    """Can this expression's value be ``null``?

    Asked of the grammar's own value routing, so the rejection keeps counting
    however much recognition upstream composes in front of it: a ternary
    answers with either branch, parentheses and a sequence with their last
    expression, ``||`` and ``??`` with their right side (a null on their left
    is exactly what both exist to pass over), ``&&`` with either side (null is
    falsy), an assignment with the value it assigns. Everything else -- a call,
    an identifier, an ``await`` -- is opaque: it may well evaluate to null, but
    the grammar does not say so, and a rejection hidden behind one is a new
    shape to be told about (`_resolvers` raises), never one to absorb.

    2.1.234 is why this is a question about *possible* answers and not the
    answer's spelling: the override default became ``return vXu(e)?xVe(t):null``
    with ``vXu`` a stub returning ``!1`` -- behaviour identical to ``return
    null`` -- and "exactly a bare null" read that build's rejection as gone.
    """
    if expr is None:
        return False
    if expr.type == "null":
        return True
    if expr.type == "ternary_expression":
        return any(
            _may_answer_null(expr.child_by_field_name(field))
            for field in ("consequence", "alternative")
        )
    if expr.type in ("parenthesized_expression", "sequence_expression"):
        parts = [child for child in js.children(expr) if child.type != "comment"]
        return bool(parts) and _may_answer_null(parts[-1])
    if expr.type == "assignment_expression":
        return _may_answer_null(expr.child_by_field_name("right"))
    if expr.type == "binary_expression":
        operator = expr.child_by_field_name("operator")
        sides = {"&&": ("left", "right"), "||": ("right",), "??": ("right",)}
        return any(
            _may_answer_null(expr.child_by_field_name(field))
            for field in sides.get("" if operator is None else operator.type, ())
        )
    return False


def _resolvers(source: Source) -> tuple[list[js.Node], list[js.Node]]:
    """Every model resolver, classified: (override, general).

    Each identity is asked positively of the arm's own ``switch``: the override
    resolver's default *can answer null* (`_may_answer_null`, over the
    default's scoped returns -- a callback's ``return`` answers for the
    callback); the general resolver's default has no answer of its own --
    absent, or with nothing scoped to return or throw -- so an unknown model
    falls out of the switch and back to the caller's passthrough. Whether
    either spells an arm as ``case"best":return f()`` or
    ``case"best":{return f()}`` is the minifier's business -- telling them
    apart by that was a silent failure waiting: brace the general resolver (a
    `let` in the arm is enough) and every family shortcut disappeared with the
    patch still green.

    The identities exclude each other by construction, and an arm holding
    neither -- a default that answers something, never null -- raises, which
    `Patch.run` turns into one broken patch naming the new shape (the promise
    `js.only` keeps for cardinality, kept here for classification). Under the
    complement this replaced, such an arm slid silently into the general list,
    and only a required step's zero was left to say anything at all.
    """
    override: list[js.Node] = []
    general: list[js.Node] = []
    for arm in _best_arms(source):
        default = next(
            (
                sibling
                for sibling in js.children(arm.parent)
                if sibling.type == "switch_default"
            ),
            None,
        )
        answers = js.every(default, js.of_type("return_statement"), scoped=True)
        refusals = js.every(default, js.of_type("throw_statement"), scoped=True)
        if not answers and not refusals:
            general.append(arm)
        elif any(_may_answer_null(_answer(statement)) for statement in answers):
            override.append(arm)
        else:
            answered = [_answer(statement) for statement in answers]
            kinds = sorted(
                {"nothing" if value is None else value.type for value in answered}
                | ({"a throw"} if refusals else set())
            )
            raise ValueError(
                "a model resolver neither rejects an unknown model nor passes "
                f"it through: its default answers {', '.join(kinds)}"
            )
    return override, general


def _existing_arms(arm: js.Node) -> set[str]:
    """The case labels already spliced in immediately after ``arm``.

    Bounded to the arms at *this* insertion point, never the whole bundle: a
    short word like ``auto`` legitimately occurs as ``case"auto":return`` in
    unrelated code, and a bundle-wide check would skip its arm here while the
    full-id arms still marked the step applied -- shipping a shortcut that
    resolves nowhere.
    """
    labels = set()
    node = arm.next_named_sibling
    while node is not None and node.type == "switch_case":
        label = node.child_by_field_name("value")
        if label is not None:
            labels.add(js.text(label)[1:-1])
        node = node.next_named_sibling
    return labels


def _arms(resolution: dict[str, str]) -> str:
    return "".join(
        f"case{js_string(name)}:return {js_string(target)};"
        for name, target in resolution.items()
    )


def _override_resolvers(source: Source) -> list[js.Node]:
    """Every resolver reached only when managed ``availableModels`` are active."""
    return _resolvers(source)[0]


def _general_resolvers(source: Source) -> list[js.Node]:
    """Every resolver an ordinary request goes through."""
    return _resolvers(source)[1]


def _register_arms(
    source: Source, arms: list[js.Node], resolution: dict[str, str], step: Outcome
) -> Source:
    """Splice resolution arms in after each of these, skipping any already there.

    An id resolves to *itself* (its identity everywhere else); a family shortcut
    resolves to the newest id in its family -- ``case"sol":return
    "gpt-5.6-sol";``, right here, before the request is built, exactly as
    ``opus`` resolves. Only shortcuts need the general resolver: an id already
    passes through it.

    Every resolver of the kind, for the reason :func:`_register` gives: a second
    ``switch`` answering `null` for an unknown model is another resolver a
    shortcut has to survive, and picking the first one to appear left the real
    one without arms -- two decoy functions were enough, at 8/8 and green.

    Skipping the arms already present is also the idempotency: a second pass
    adds nothing rather than a duplicate arm the switch would never reach.
    """
    edits = []
    for arm in arms:
        step.candidates += 1
        add = {
            name: target
            for name, target in resolution.items()
            if name not in _existing_arms(arm)
        }
        step.applied += 1
        if add:
            edits.append(Edit.after(arm, _arms(add)))
    return source.apply(edits)


# -------------------------------------------------------------------- route


def _builds_url(node: js.Node) -> bool:
    """Is this the call that assembles the request URL?

    By the method it calls, not by the receiver in front of it: `this` is where
    the helper lives today and says nothing about what the call does.
    """
    return node.type == "call_expression" and js.reads(
        node.child_by_field_name("function"), _BUILD_URL
    )


def _build_request(source: Source) -> js.Node | None:
    """The SDK method that assembles a request -- the one that builds the URL.

    Several methods carry the name -- two to four across the corpus -- and all
    but one only delegate to ``super``. The one meant here is identified by
    what it does, which is why the count is free to move.
    """
    return js.only(
        [
            method
            for node in source.find(_BUILD_REQUEST)
            for method in [js.up(node, "method_definition")]
            if method is not None
            and method.child_by_field_name("name") == node
            and js.first(method, _builds_url) is not None
        ],
        "request builders",
    )


def _redirect(source: Source, options: Options, step: Outcome) -> Source:
    """Swap the request origin to the gateway, for the chosen ids only."""
    method = _build_request(source)
    if method is None:
        return source
    body = js.body(method)
    built = js.only(
        js.every(
            body,
            lambda n: (
                n.type == "variable_declarator"
                and _builds_url(n.child_by_field_name("value") or n)
            ),
            scoped=True,
        ),
        "URLs built in this request builder",
    )
    if built is None:
        return source
    # The options copy the request is assembled from, named by the destructuring
    # that takes the request's parts out of it -- and taken from a scope the
    # injected test can see. Both names are spliced into a statement of the
    # method's own, so a nested helper that happens to destructure the same
    # property binds a name that does not exist there: the block read
    # `if(z.body&&...)` off a helper's parameter, at `redirect` 1/1 and green,
    # and every request the method built threw.
    statement = js.declared(built)
    if statement is None:
        return source
    copies = js.every(
        body,
        lambda n: (
            n.type == "variable_declarator"
            and (name := n.child_by_field_name("name")) is not None
            and name.type == "object_pattern"
            and _DEFAULT_BASE_URL in js.props(name)
            # Destructured from a *name* the method already holds, because that
            # name is what the injected test reads three times; an expression
            # there would be evaluated three times over, per request.
            and (taken_from := n.child_by_field_name("value")) is not None
            and taken_from.type == "identifier"
        ),
    )
    taken = js.only(
        [copy for copy in copies if js.visible(copy, statement)],
        "request option copies",
    )
    if taken is None:
        return source
    url = js.text(built.child_by_field_name("name"))
    opts = js.text(taken.child_by_field_name("value"))

    step.candidates += 1
    routed = "[" + ",".join(js_string(m.id) for m in options.codex_models) + "]"
    # The URL is all this changes, and a diverted request still carries the
    # Anthropic credential: measured on the wire, `Authorization: Bearer
    # sk-ant-oat01-...` and the whole prompt arrive at whatever holds the
    # gateway port. Our gateway never reads it; a process that squatted the port
    # would.
    #
    # Stripping it from here does not work, and the obvious attempt is worth
    # recording so it is not retried blind. `options.headers` is the last source
    # `buildHeaders` merges and its merge treats `null` as delete, so setting
    # either a null or an inert value there should win. Neither reaches the
    # wire: with the block proven to run (a marker spliced into the path
    # arrived), a probe header set on the options object *and* on the local copy
    # was absent from the request both times. Something between `buildRequest`
    # and `fetch` discards `options.headers` on this build. A real fix therefore
    # needs its own anchor further down -- `prepareRequest`, which receives the
    # final `Headers` object and the URL -- which is a new required step and new
    # matcher surface, not a line added here.
    step.applied += 1
    return source.apply(
        [
            Edit.after(
                statement,
                f'if({opts}.body&&typeof {opts}.body=="object"&&'
                f"{routed}.includes({opts}.body.model))"
                f"{url}={url}.replace({_ORIGIN},"
                f'"http://127.0.0.1:{options.codex_port}");',
            )
        ]
    )


# ------------------------------------------------------------------- display


def _display_name(handle: str) -> str:
    """A handle spelled as a name: ``gpt-5.7-sol`` -> ``GPT 5.7 Sol``, ``sol`` -> ``Sol``.

    Derived from the handle itself (or from the name discovery reported, which
    falls back to it), never from a catalogue of ours -- so a model released
    tomorrow reads correctly without patch-cc having heard of it. Any word that
    already carries case or digits is left exactly as it came, which makes this a
    no-op on a backend that starts sending real titles.
    """
    return " ".join(
        # `GPT` is OpenAI's own capitalization of the prefix. Every other
        # all-lowercase word title-cases; `5.7`, having no cased characters, is
        # not lowercase and falls through untouched.
        "GPT" if word == "gpt" else word.capitalize() if word.islower() else word
        for word in handle.replace("-", " ").split()
    )


def _describe(model) -> str:
    """One Codex model as the ``/model`` picker's second line."""
    window = f" with {model.context // 1000}k context" if model.context else ""
    return f"{_display_name(model.label)}{window} from codex"


def _row_assembler(source: Source) -> js.Node | None:
    """The ``/model`` picker's choke point: the call every row list ends at.

    Every branch of the picker builds its own list and hands it here to have the
    default rows appended, so this is the one place all of them pass through.
    It is identified by what it does -- take a list, add to it in a loop, and
    give it back -- and by the model names it decides between, which are the
    API's vocabulary rather than anything minified.

    One function, or none: rows spliced into a list some other function keeps
    is not a weaker version of this rewrite, it is a different list growing
    entries nobody asked it for.
    """
    found: list[js.Node] = []
    for node in source.find('"opus"'):
        assembler = js.climb(node, lambda n: n.type in js.FUNCTIONS)
        taken = js.positional(assembler) if assembler is not None else []
        if assembler is None or not taken:
            continue
        block = js.body(assembler)
        if block is None or js.first(block, js.of_type("for_in_statement")) is None:
            continue
        returned = js.first(block, js.returns(js.text(js.binding(taken[0]))))
        if returned is not None and returned.id not in {node.id for node in found}:
            found.append(returned)
    return js.only(found, "row assemblers")


def _register_picker(
    source: Source, models, shorts: dict[str, str], step: Outcome
) -> Source:
    returned = _row_assembler(source)
    if returned is None:
        return source
    step.candidates += 1
    # The node the return answers with, not its text with the keyword sliced off
    # and a semicolon this build may or may not emit trimmed back: `js.returns`
    # already proved the shape, so reaching back through the spelling is a
    # second and weaker claim about a node already in hand.
    array = js.text(js.children(returned)[0])
    by_id = {m.id: m for m in models}
    covered = set(shorts.values())
    # (value you pick, the model it stands for), shortcuts first. A shortcut *is*
    # its family's newest model, so listing that model's id underneath would offer
    # the same choice twice; only a model no shortcut speaks for gets its own row
    # -- an older version kept alongside a newer one, or a slug with no family at
    # all (`gpt-5.5`). Nothing is hidden by that: the description names the
    # concrete model, and the id stays typeable either way.
    picks = [(short, by_id[target]) for short, target in shorts.items()]
    picks += [(m.id, m) for m in models if m.id not in covered]
    # Shaped like the binary's own rows: the label is the handle *spelled as a
    # name* -- `Sol` for a shortcut, `GPT 5.6 Sol` for an id -- and the description
    # reads `<model> with <n> context`, as the native rows do. One rule for both
    # kinds, because a row is a row: labelling id rows with the raw slug made them
    # the only entries in the list wearing a different sort of name than every
    # neighbour, `gpt-5.6-sol` sitting under `Opus`. It ends in "from codex"
    # because this is the only place in the picker where a model leaves Anthropic,
    # and nothing else in the list would say so.
    entries = ",".join(
        f"{{value:{js_string(value)},label:{js_string(_display_name(value))},"
        f"description:{js_string(_describe(model))}}}"
        for value, model in picks
    )
    # Dedup is left to the runtime `.some()` guard rather than a build-time
    # check: a short value like `auto` occurs as `value:"auto"` in the theme
    # picker, so a per-value check would false-skip its row while the id rows
    # still marked the step applied. It cannot matter in practice either -- the
    # patcher always runs from a pristine source, never its own output.
    step.applied += 1
    return source.apply(
        [
            Edit.before(
                returned,
                f"[{entries}].forEach(function(__cc_row){{"
                f"if(!{array}.some(function(__cc_seen){{"
                f"return __cc_seen.value===__cc_row.value}}))"
                f"{array}.push(__cc_row)}});",
            )
        ]
    )


def _window_resolver(source: Source) -> js.Node | None:
    """The function that answers with a model's context window.

    Identity is what it does with the env override, which is the question the
    table answers too: it *reads* ``CLAUDE_CODE_MAX_CONTEXT_TOKENS`` and
    *returns* what it read -- the same "the value admitted is the value
    returned" proof `max-effort`'s whitelist and the picker's row assembler are
    identified by. Two other functions read the same name and neither is this
    one: the compaction override answers with it but takes no model to key on,
    and the unknown-model warning reads it only to decide whether to complain.

    The read must be a *member* read. Climbing from any occurrence of the name
    took the first one in the bundle, which is a key in esbuild's export map --
    so the climb landed on the CommonJS module wrapper, whose five parameters
    satisfied every check, and the window table was spliced into byte 91 of the
    bundle keyed on ``String(exports)``. It reported ``candidates=1 applied=1``
    on every build in the corpus and never once answered a question.
    """
    found = []
    for node in source.find(_MAX_CONTEXT):
        read = js.up(node, "member_expression")
        if read is None or read.child_by_field_name("property") != node:
            continue
        resolver = js.climb(read, lambda n: n.type in js.FUNCTIONS)
        declared = js.up(read, "variable_declarator")
        window = declared.child_by_field_name("name") if declared is not None else None
        if resolver is None or window is None or not js.positional(resolver):
            continue
        if js.first(resolver, js.returns(js.text(window))) is not None:
            found.append(resolver)
    return js.only(found, "context-window resolvers")


def _register_context(source: Source, models, outcome: Outcome) -> Source:
    """Bake the real context window for each chosen model that reports one.

    Takes the parent outcome, not a step, so the step exists only when there is a
    window to bake. With none, no rewrite is owed and a step would have to report
    that as either an absent shape or a missed rewrite -- both untrue, and both
    reading as a build problem rather than as nothing to do (docs/CONDUCT.md).
    """
    windows = {m.id: m.context for m in models if m.context > 0}
    if not windows:
        outcome.note("no chosen model reports a context window; 200k default stands")
        return source

    # There *is* a window to bake, so the step exists before the search for the
    # place to bake it -- a drifted window resolver then reads as this step
    # finding nothing, not as no step at all. Created *after* the search, a
    # renamed CLAUDE_CODE_MAX_CONTEXT_TOKENS produced seven steps, no note and no
    # absent-step line, the exact silence `Outcome.declare` now exists to break.
    outcome.declare(optional=("context",))
    step = outcome.step("context")
    resolver = _window_resolver(source)
    body = js.body(resolver) if resolver is not None else None
    if body is None or not body.named_children:
        return source

    step.candidates += 1
    model_var = js.text(js.binding(js.positional(resolver)[0]))
    # `__proto__:null` in the literal, so the table is only the ids it holds. An
    # ordinary object literal inherits `Object.prototype`, and the lookup below is
    # keyed on a name from outside: `constructor` is already lowercase, so it
    # would answer with a *function*, pass the `!==void 0` guard, and be returned
    # as a context window. The guard is not the fix -- a table with nothing behind
    # it is, and it dissolves the case rather than testing for it. Quoted is the
    # spelling json emits and still sets the prototype (measured on
    # JavaScriptCore, which is what Bun runs, and on V8).
    table = json.dumps({"__proto__": None, **windows}, separators=(",", ":"))
    # Before the resolver's first statement, so it answers ahead of everything
    # below it -- the 200k default this function ends on, and its own
    # CLAUDE_CODE_MAX_CONTEXT_TOKENS read, which applies to exactly the non-Claude
    # models we are registering. It does not (and should not) outrank the caller's
    # own env override or long-context clamp, which are decided before this
    # function is reached. Only our ids are answered; an unknown key falls
    # through untouched.
    step.applied += 1
    return source.apply(
        [
            Edit.before(
                body.named_children[0],
                f"var __cc_window=({table})"
                f'[String({model_var}||"").trim().toLowerCase()];'
                f"if(__cc_window!==void 0)return __cc_window;",
            )
        ]
    )


#: Advisor rank claimed for every registered Codex model: Opus 5's own rank in
#: the registry (Fable is 5, Sonnet 5 is 3, Haiku 1). Rank *presence* is what
#: `/advisor <model>` checks for eligibility; the number only orders
#: "at least as capable" pairings. The chosen ids are frontier coding models,
#: so they sit with Opus -- able to advise anything below Fable, claiming
#: nothing above it. Per-model precision here would be a catalogue of ours:
#: the plan reports no rank, so one honest number beats four invented ones.
_ADVISOR_RANK = 4

#: The two effort rungs the registry names with a capability of their own; the
#: base ladder (low/medium/high) is the bare ``effort`` capability. A level
#: upstream adds above ``max`` is one entry here, not a new branch.
_EFFORT_CAPABILITIES = {"xhigh": "xhigh_effort", "max": "max_effort"}


def _effort_capabilities(model) -> list[str]:
    """The registry capability strings the plan's effort list vouches for.

    Positive declarations only. The binary reads an *absent* capability as "ask
    the provider fallback" (permissive on the first-party API), never as "no" --
    so leaving one out keeps today's behaviour, and a wrong *yes* is the only
    mistake this could bake. Which is why the list is built from the plan's own
    ``supported_reasoning_levels`` and stops at the effort trio: the thinking
    and adaptive flags change how the binary builds requests, and those belong
    to models Anthropic ships.

    The *list* is always emitted, empty included -- an absent capability is a
    fallback, an absent list is a crash (see :func:`_registry_entry`).
    """
    if not model.efforts:
        return []
    return [
        "effort",
        *(
            capability
            for level, capability in _EFFORT_CAPABILITIES.items()
            if level in model.efforts
        ),
    ]


def _registry_entry(model) -> str:
    """One schema-valid registry record for ``model``, as JS source.

    The registry's zod parse is all-or-nothing with an **empty** fallback: one
    malformed entry and every model -- Claude's included -- loses its metadata
    (names, capabilities, aliases). So this emits only fields the schema
    declares, shaped exactly as it demands: the required four (``id``,
    ``family``, ``display_name``, ``provider_ids.first_party``), plus
    ``capabilities``, and beyond them only values with something true to
    record. JSON is the emitter for the reason
    :func:`patch_cc.patches.base.js_string` gives: valid JSON is valid JS,
    escaping included.

    ``capabilities`` is emitted even when the plan reported no efforts, because
    the schema's ``.default([])`` never runs: the runtime catalogue is built
    from the raw literal, and its capability check reads
    ``entry?.capabilities.includes(name)`` unguarded (2.1.285). An entry
    without the array ended the session at its first capability question --
    ``An internal error ended the session (undefined is not an object
    (evaluating 'Ga(h(e))?.capabilities.includes'))`` on the first turn of an
    offline bake -- while an empty array answers every question "not
    declared", which is the provider fallback :func:`_effort_capabilities`
    describes.

    ``pricing`` and ``max_output_tokens`` are left out on purpose -- their
    readers all guard for absence, a subscription has no per-token price to
    state, and the output cap never reaches the wire (the backend refuses
    explicit caps; the translator drops them). ``default_effort`` is left out
    too, and that one is a decision, not a shrug: the binary resolves a missing
    default as ``high`` (``?.default_effort??"high"``) -- the very default its
    own flagships declare -- so omission makes ``/effort auto`` and an untouched
    session mean on these models exactly what they mean on Opus. Baking the
    plan's default made auto on sol mean *low*: Codex-the-product's UX imported
    into a harness that has its own convention. A model that some day lacks
    even ``high`` still cannot dead-turn -- the gateway clamps the refusal.
    """
    entry: dict = {
        "id": model.id,
        "family": family_of(model.id) or model.id,
        "display_name": _display_name(model.label),
        "provider_ids": {"first_party": model.id},
        "advisor_rank": _ADVISOR_RANK,
        "capabilities": _effort_capabilities(model),
    }
    return json.dumps(entry, separators=(",", ":"))


def _model_table(source: Source) -> js.Node | None:
    """The binary's own model table: the ``models`` array beside ``aliases``.

    One embedded object holds everything Claude Code knows about a model it did
    not hardcode a check for. It is found by the pair of properties that make it
    that table, and confirmed by its entries carrying the fields every record
    has -- so the two look-alikes in the bundle (a ``models`` built by a call,
    and an empty one) are excluded by what they contain rather than by where
    they sit.

    One table, or none: a second one is a question about which of them the
    status line reads, and adding models to the wrong one is how they would be
    accepted everywhere and named nowhere.
    """
    found = []
    for node in source.find(_ALIASES):
        pair = js.named(node)
        if pair is None:
            continue
        table = js.owner(pair)
        array = js.props(table).get(_MODELS) if table is not None else None
        if array is None or array.type != "array":
            continue
        first = next(iter(js.elements(array)), None)
        if first is not None and js.carries(first, *_REGISTRY_FIELDS):
            found.append(array)
    return js.only(found, "model tables")


def _register_registry(source: Source, models, step: Outcome) -> Source:
    """Add each chosen model to the binary's own model table.

    This is what makes the models first-class rather than merely accepted:
    every consumer of the registry -- the status line's display name, the
    effort capability checks, `/advisor` eligibility, and any surface neither
    we nor upstream have enumerated -- handles them by default from here on.
    The picker and the context resolver are *not* registry-driven (rows are
    hand-built upstream; the window resolver ends on a flat 200k), which is why
    those two steps still exist alongside this one.
    """
    array = _model_table(source)
    if array is None:
        return source
    step.candidates += 1
    entries = js.elements(array)
    if not entries:
        return source
    step.applied += 1
    return source.apply(
        [
            Edit.after(
                entries[-1],
                "," + ",".join(_registry_entry(model) for model in models),
            )
        ]
    )


def _codex_models(source: Source, options: Options, outcome: Outcome) -> Source:
    """Register and route the chosen Codex models.

    A no-op with no models configured -- like ``subagent-models``, the patch is
    only meaningful once something has been chosen.
    """
    if not options.codex_models:
        outcome.note("no codex models chosen; pick some in `patch-cc` or with --codex")
        return source

    # Fail closed on a would-brick collision. An id the bundle already claims
    # registers as a duplicate the registry's zod parse refuses at first use --
    # bricking every command -- or as a resolver no-op that hijacks that model.
    # The CLI refuses these up front (cli._codex_selection); this is the backstop
    # that keeps CONDUCT's promise about the binary on the paths that do not pass
    # through it, and it is derived from the bundle rather than remembered.
    collisions = sorted(
        m.id for m in options.codex_models if m.id in claimed_model_names(source)
    )
    if collisions:
        outcome.note(
            "refused: chosen id(s) collide with a model the bundle already claims "
            f"and would brick the binary: {', '.join(collisions)}"
        )
        return source

    model_ids = [m.id for m in options.codex_models]
    # Classified once here for the gate and the note; the registrations below
    # re-derive from the source each hands the next, since every batch of edits
    # is a new parse. A count that moves on a green run is the early warning.
    override, general = _resolvers(source)
    outcome.note(f"resolvers: {len(override)} override, {len(general)} general")
    # Family shortcuts (`sol` -> the newest `gpt-<ver>-sol`) resolve to an id
    # before the request is built, so they ride only the surfaces that accept,
    # resolve, and show a model -- validator, both resolvers, picker. They need no
    # `enum` (subagent pins stay explicit, pinned to a stable id, not a moving
    # "newest") and no `redirect`/`context` entry (resolution rewrites them to the
    # id first). They are gated as a unit on the general resolver: if that anchor
    # ever drifts, a `/model sol` would be accepted but never resolved -- a 404 --
    # so absent it, no shortcut is registered anywhere and the ids (which need none
    # of this) carry on untouched.
    shorts = family_aliases(options.codex_models)
    if shorts and not general:
        outcome.note("general model resolver anchor drifted; shortcuts skipped")
        shorts = {}

    # `general-resolver` is owed exactly when a shortcut is; `context` declares
    # itself where its own work is owed (see _register_context).
    outcome.declare(
        required=(
            "enum",
            "validator",
            "resolver",
            *(("general-resolver",) if shorts else ()),
            "redirect",
        ),
        optional=("picker", "registry"),
    )
    source = _register(source, model_enums(source), model_ids, outcome.step("enum"))
    source = _register(
        source,
        _validators(source),
        model_ids + list(shorts),
        outcome.step("validator"),
    )
    source = _register_arms(
        source,
        _override_resolvers(source),
        {i: i for i in model_ids} | shorts,
        outcome.step("resolver"),
    )
    if shorts:
        source = _register_arms(
            source,
            _general_resolvers(source),
            shorts,
            outcome.step("general-resolver"),
        )
    source = _redirect(source, options, outcome.step("redirect"))
    source = _register_picker(
        source, options.codex_models, shorts, outcome.step("picker")
    )
    source = _register_context(source, options.codex_models, outcome)
    return _register_registry(source, options.codex_models, outcome.step("registry"))


def _ids_from(value: object) -> list[CodexModel]:
    """Codex models from a stored id list -- ids only, as both stores keep them.

    Deduplicated at the boundary, because a *store* is untrusted input the way a
    command line is not: the CLI already collapses a repeated ``--codex`` with
    ``dict.fromkeys`` (cli._requested), but the cache and the manifest are files
    a hand can edit, and one id listed twice bakes two identical registry
    entries -- a duplicate ``provider_ids`` the registry's own ``safeParse``
    rejects with ``provider id collision across distinct entries``, bricking
    every command (the very outcome `claimed_model_names` guards the *bundle's*
    names against, which cannot see a duplicate among the *chosen* ids). One
    home for parsing a stored id list, so `--from-cache` and a seeded menu
    cannot smuggle in the collision the front door refuses -- the same reason
    `is_valid_port` refuses a hand-edited ``"port": true``.
    """
    items = value if isinstance(value, list) else []
    ids = dict.fromkeys(i for i in items if isinstance(i, str) and i)
    return [CodexModel(i) for i in ids]


def _codex_from_manifest(options: Options, value: object) -> None:
    codex = value if isinstance(value, dict) else {}
    options.codex_models = _ids_from(codex.get("models"))
    port = codex.get("port")
    options.codex_port = port if is_valid_port(port) else DEFAULT_PORT


def _codex_from_cache(options: Options, cache: dict[str, object]) -> None:
    options.codex_models = _ids_from(cache.get("codex_models"))
    port = cache.get("codex_port")
    options.codex_port = port if is_valid_port(port) else DEFAULT_PORT


#: Codex is the setting whose two stores differ in *shape*: the manifest nests
#: the port beside the ids (one ``codex`` object), the cache keeps two flat keys.
#: A model's name and window are the plan's to report and are never stored, so
#: only ids ride here -- the same rule the gateway follows.
_CODEX_SETTING = Setting(
    manifest_key="codex",
    recorded=lambda o: bool(o.codex_models),
    to_manifest=lambda o: {
        "port": o.codex_port,
        "models": [m.id for m in o.codex_models],
    },
    from_manifest=_codex_from_manifest,
    to_cache=lambda o: {
        "codex_models": [m.id for m in o.codex_models],
        "codex_port": o.codex_port,
    },
    from_cache=_codex_from_cache,
)


PATCHES = [
    Patch(
        id="codex-models",
        title="Codex models",
        summary="Register your OpenAI/Codex models and route them to the local gateway.",
        group=GROUP_MODELS,
        fn=_codex_models,
        default=False,
        option="--codex",
        anchors=(
            "Optional model override",
            '"opusplan"',
            f"case{_BEST}:",
            f"{_DEFAULT_BASE_URL}:",
            _MAX_CONTEXT,
            f"{_ALIASES}:",
        ),
        setting=_CODEX_SETTING,
    ),
]
