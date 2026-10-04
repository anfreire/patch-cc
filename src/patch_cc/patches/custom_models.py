"""Register custom models and route their Anthropic requests to an endpoint."""

from __future__ import annotations

import json
from dataclasses import replace

from .. import js
from ..custom_models import (
    KEY_ENV,
    ConfigurationError,
    CustomModel,
    aliases,
    endpoint,
    key_path,
    model_names,
    model_values,
    models_from,
)
from ..js import Edit, Source
from .agents import model_enums
from .base import GROUP_MODELS, Options, Outcome, Patch, Setting, js_string

# --- anchors: authored names and the grammar around them (docs/PLAYBOOK.md) ---

#: The known-model master list. Found by membership -- the built-in names it
#: holds -- never by their order: the array on 2.1.232 interleaves ``"best"``
#: and three ``[1m]`` variants between ``"sonnet"`` and ``"opusplan"``, so a
#: spelled sequence is one upstream reshuffle from death.
_BUILT_IN_MODELS = ("sonnet", "opus", "haiku", "opusplan")

_BEST = '"best"'
_ALIASES = "aliases"
_MODELS = "models"
_REGISTRY_FIELDS = ("id", "family", "display_name")
_BUILD_REQUEST = "buildRequest"
_BUILD_URL = "buildURL"
_DEFAULT_BASE_URL = "defaultBaseURL"
_MAX_CONTEXT = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
_CAPABILITIES = "CLAUDE_CODE_MODEL_CAPABILITIES"


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
    meant nor can be starved by a second one. Taking the first match would let
    a decoy carrying the four built-in model names absorb the whole
    registration: real list untouched, ids nowhere, every step green.

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

    Registering a chosen custom id that collides with one of these is what bricks
    the binary or hijacks a real model, so it is what the id must be refused
    against -- *derived* from the bundle in hand, never a hardcoded snapshot that
    upstream can outgrow (docs/PLAYBOOK.md):

    * a duplicate ``provider_ids`` value makes the catalogue builder
      throw ``provider id collision across distinct entries`` at first use, so
      every command dies -- and 13 of these on 2.1.233 (``us.anthropic.claude-
      opus-5`` and kin) are valid slugs that the ``claude-`` prefix guard never
      catches;
    * a name a validator or resolver already owns (``opus``) registers as a
      no-op the step counts as landed, then bakes ``["opus"].includes(...)`` into
      the redirect and diverts that model's own requests to the endpoint.

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


def validate_models(source: Source, models: list[CustomModel]) -> list[CustomModel]:
    """One collision check for typed, discovered and saved model selections."""
    models = models_from(model_values(models))
    claimed = claimed_model_names(source)
    collisions = sorted(name for name in model_names(models) if name in claimed)
    if collisions:
        raise ConfigurationError(
            "custom model ids or aliases collide with the binary's own names: "
            + ", ".join(collisions)
        )
    return models


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
    answer's spelling: the override default there is ``return f(e)?g(t):null``
    with ``f`` a stub returning ``!1`` -- behaviour identical to ``return
    null`` -- and "exactly a bare null" reads that build's rejection as gone.
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
    apart by that is a silent failure waiting: brace the general resolver (a
    `let` in the arm is enough) and every shortcut disappears with the patch
    still green.

    The identities exclude each other by construction, and an arm holding
    neither -- a default that answers something, never null -- raises, which
    `Patch.run` turns into one broken patch naming the new shape (the promise
    `js.only` keeps for cardinality, kept here for classification). Classified
    as the complement of the other, such an arm would slide silently into the
    general list, with only a required step's zero left to say anything at all.
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

    An id resolves to *itself* (its identity everywhere else); an alias
    resolves to its model's id -- ``case"coder":return "qwen3-coder";``, right
    here, before the request is built, exactly as ``opus`` resolves. Only
    aliases need the general resolver: an id already passes through it.

    Every resolver of the kind, for the reason :func:`_register` gives: a second
    ``switch`` answering `null` for an unknown model is another resolver a
    shortcut has to survive, and picking the first one to appear would leave the
    real one without arms -- two decoy functions are enough, at 8/8 and green.

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
    """Replace the SDK base URL, preserving the API path, for selected models only."""
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
    # injected test can see. The name is spliced into a statement of the
    # method's own, so a nested helper that happens to destructure the same
    # property binds a name that does not exist there: a test reading
    # `z.body&&...` off a helper's parameter would be `redirect` 1/1 and green,
    # and every request the method builds would throw.
    value = built.child_by_field_name("value")
    if value is None:
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
        [copy for copy in copies if js.visible(copy, value)],
        "request option copies",
    )
    if taken is None:
        return source
    opts = js.text(taken.child_by_field_name("value"))

    step.candidates += 1
    step.applied += 1
    # The built URL is wrapped where it is produced; the binding is never
    # assigned to. Whether it is a `let`, a `const` or a `var` is the minifier's
    # choice, and an assignment to a `const` parses, bakes, boots `--version`
    # and throws on every routed request -- a death no step could report. An
    # arrow keeps the method's `this`, and the URL is parsed only for a request
    # that is diverted.
    return source.apply(
        [
            Edit.replace(
                value,
                f"((__cc_url)=>{{if(!({_diverted(options, opts)}))return __cc_url;"
                "var __cc_u=new URL(__cc_url),"
                r'__cc_base=new URL(this.baseURL).pathname.replace(/\/+$/,"");'
                f"return {js_string(options.endpoint)}+"
                f"__cc_u.pathname.slice(__cc_base.length)+__cc_u.search}})({js.text(value)})",
            )
        ]
    )


def _routed(options: Options, value: str) -> str:
    names = model_names(options.custom_models)
    return f"{json.dumps(names, separators=(',', ':'))}.includes({value})"


def _diverted(options: Options, value: str) -> str:
    return f'{value}&&{value}.body&&typeof {value}.body=="object"&&{_routed(options, value + ".body.model")}'


def _credential(source: Source, options: Options, step: Outcome) -> Source:
    """Replace credentials after the SDK has finished preparing its headers."""
    methods = []
    for node in source.find("prepareRequest"):
        method = js.up(node, "method_definition")
        if (
            method is not None
            and method.child_by_field_name("name") == node
            and {"url", "options"} <= js.parameters(method).keys()
            and js.first(
                js.body(method), lambda n: js.reads(n, "_authState"), scoped=True
            )
            is not None
        ):
            methods.append(method)
    method = js.only(methods, "credential preparation hooks")
    if method is None:
        return source
    body = js.body(method)
    params = js.positional(method)
    if body is None or not params:
        return source
    # A hook that exits before its tail needs a new semantic location, not an
    # insertion that merely parses and leaves the original bearer on the wire.
    if js.first(body, js.of_type("return_statement"), scoped=True) is not None:
        raise ValueError("credential hook returns before final header preparation")
    request = js.text(js.binding(params[0]))
    opts = js.text(js.parameters(method)["options"])
    step.candidates += 1
    step.applied += 1
    code = (
        f"if({_diverted(options, opts)}){{"
        f"{request}.headers=new Headers({request}.headers);"
        f'{request}.headers.delete("authorization");'
        f'{request}.headers.delete("x-api-key");'
        f"var __cc_key=process.env.{KEY_ENV};"
        f"if(__cc_key==null)try{{__cc_key="
        f'process.getBuiltinModule("fs").readFileSync({js_string(str(key_path()))},"utf8").trim()'
        f"}}catch{{}}"
        f'if(__cc_key){{{request}.headers.set("authorization","Bearer "+__cc_key);'
        f'{request}.headers.set("x-api-key",__cc_key)}}}}'
    )
    return source.apply([Edit.before(body.children[-1], code)])


def _stateless(source: Source, options: Options, outcome: Outcome) -> Source:
    """Use the planner's own per-model stateless input before history is sliced."""
    outcome.declare(
        required=("stateless",) if source.count("thread_unsupported_request") else (),
        optional=("stateless",),
    )
    step = outcome.step("stateless")
    edits = []
    for node in source.find("modelHeldStateless"):
        pair = js.named(node)
        obj = js.owner(pair) if pair is not None else None
        if obj is None or obj.type != "object":
            continue
        props = js.props(obj)
        if "model" not in props:
            continue
        value = props["modelHeldStateless"]
        step.candidates += 1
        edits.append(
            Edit.replace(
                value,
                f"({js.text(value)}||{_routed(options, js.text(props['model']))})",
            )
        )
        step.applied += 1
    return source.apply(edits)


def _capability_gate(source: Source, capability: str) -> js.Node | None:
    """The native per-model predicate querying a canonical model's capability.

    Registry rows and error recognizers carry the same literal. The predicate
    queries it with a local model obtained by normalizing its own first argument.
    This identity holds before and after upstream added capability env overrides.
    """
    found = {}
    for node in source.find(js_string(capability)):
        args_node = node.parent
        if args_node is None or args_node.type != "arguments":
            continue
        call = args_node.parent
        args = js.arguments(call)
        if len(args) < 2 or args[1] != node or args[0].type != "identifier":
            continue
        fn = js.up(call, *js.FUNCTIONS)
        params = js.positional(fn)
        body = js.body(fn)
        if fn is None or not params or body is None or body.type != "statement_block":
            continue
        first_param = js.text(js.binding(params[0]))

        def normalizes(
            n: js.Node,
            expected: str = js.text(args[0]),
            parameter: str = first_param,
            scope: js.Node = body,
        ) -> bool:
            return (
                n.type == "variable_declarator"
                and (name := n.child_by_field_name("name")) is not None
                and js.text(name) == expected
                and (value := n.child_by_field_name("value")) is not None
                and value.type == "call_expression"
                and len(js.arguments(value)) == 1
                and js.text(js.arguments(value)[0]) == parameter
                and js.visible(n, scope)
            )

        normalizers = js.every(body, normalizes, scoped=True)
        if normalizers:
            found[fn.id] = fn
    return js.only(list(found.values()), f"{capability} model predicates")


def _classic(source: Source, options: Options, outcome: Outcome) -> Source:
    # The feature is required on every supported build. Absence of its locator
    # cannot turn the requirement off and disguise drift as an older build.
    for capability in ("mid_conv_system", "context_management"):
        name = f"classic:{capability}"
        outcome.declare(required=(name,))
        step = outcome.step(name)
        fn = _capability_gate(source, capability)
        body = js.body(fn)
        if fn is None or body is None or not body.named_children:
            continue
        model = js.text(js.binding(js.positional(fn)[0]))
        step.candidates += 1
        step.applied += 1
        source = source.apply(
            [
                Edit.before(
                    body.named_children[0], f"if({_routed(options, model)})return!1;"
                )
            ]
        )
    return source


def _effort_limits(source: Source, options: Options, outcome: Outcome) -> Source:
    entries: list[str] = []
    for model in options.custom_models:
        denied = [
            cap
            for level, cap in _EFFORT_CAPABILITIES.items()
            if level not in model.efforts
        ]
        if not model.efforts:
            denied.insert(0, "effort")
        if denied:
            entries.extend(
                name + "=" + ",".join("-" + cap for cap in denied)
                for name in model_names([model])
            )
    if not entries:
        return source
    outcome.declare(
        required=("effort-limits",) if source.count(_CAPABILITIES) else (),
        optional=("effort-limits",),
    )
    step = outcome.step("effort-limits")
    if not step.expect:
        outcome.note(
            "this build has no per-model capability override; effort limits skipped"
        )
    reads = [
        node.parent
        for node in source.find(_CAPABILITIES)
        if js.reads(node.parent, _CAPABILITIES) and not js.written(node.parent)
    ]
    read = js.only(reads, "per-model capability overrides")
    if read is None:
        return source
    step.candidates += 1
    step.applied += 1
    return source.apply(
        [
            Edit.replace(
                read, f'({js_string(";".join(entries) + ";")}+({js.text(read)}??""))'
            )
        ]
    )


# ------------------------------------------------------------------- display


def _display_name(handle: str) -> str:
    """A handle spelled as a name: ``qwen3-coder`` -> ``Qwen3 Coder``, ``sol`` -> ``Sol``.

    Derived from the handle itself (or from the name discovery reported, which
    falls back to it), never from a catalogue of ours -- so a model released
    tomorrow reads correctly without patch-cc having heard of it. Any word that
    already carries case or digits is left exactly as it came, which makes this a
    no-op on a backend that starts sending real titles.
    """
    return " ".join(
        word.capitalize() if word.islower() else word
        for word in handle.replace("-", " ").split()
    )


def _describe(model) -> str:
    """One custom model as the ``/model`` picker's second line."""
    window = f" with {model.context // 1000}k context" if model.context else ""
    return f"{_display_name(model.label)}{window} via custom endpoint"


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


def _register_picker(source: Source, models, step: Outcome) -> Source:
    returned = _row_assembler(source)
    if returned is None:
        return source
    step.candidates += 1
    # The node the return answers with, not its text with the keyword sliced off
    # and a semicolon this build may or may not emit trimmed back: `js.returns`
    # already proved the shape, so reaching back through the spelling is a
    # second and weaker claim about a node already in hand.
    array = js.text(js.children(returned)[0])
    # An alias is a command shortcut, not another model or a replacement label.
    # One picker row per model keeps its real id and chosen display name.
    entries = ",".join(
        f"{{value:{js_string(model.id)},label:{js_string(_display_name(model.label))},"
        f"description:{js_string(_describe(model))}}}"
        for model in models
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

    The read must be a *member* read. The first occurrence of the name in the
    bundle is a key in esbuild's export map: climbed from there, the resolver is
    the CommonJS module wrapper, whose five parameters satisfy every check, and
    the window table lands at byte 91 of the bundle keyed on ``String(exports)``
    -- ``candidates=1 applied=1`` on every build in the corpus, and never once
    an answer to a question.
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
    windows = {
        name: model.context
        for model in models
        if model.context > 0
        for name in model_names([model])
    }
    if not windows:
        outcome.note("no chosen model reports a context window; 200k default stands")
        return source

    # There *is* a window to bake, so the step exists before the search for the
    # place to bake it -- a drifted window resolver then reads as this step
    # finding nothing, not as no step at all. Created *after* the search, a
    # renamed CLAUDE_CODE_MAX_CONTEXT_TOKENS would leave the other steps, no
    # note and no absent-step line: the silence `Outcome.declare` exists to break.
    outcome.declare(required=("context",))
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
    # Before the native fallback: registry-known ids can bypass the environment
    # override, so their explicit per-model window must be answered here.
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


#: The two effort rungs the registry names with a capability of their own; the
#: base ladder (low/medium/high) is the bare ``effort`` capability. A level
#: upstream adds above ``max`` is one entry here, not a new branch.
_EFFORT_CAPABILITIES = {"xhigh": "xhigh_effort", "max": "max_effort"}


def _effort_capabilities(model) -> list[str]:
    """The registry capability strings the endpoint's advertised effort list vouches for.

    Positive declarations only. The binary reads an *absent* capability as "ask
    the provider fallback" (permissive on the first-party API), never as "no" --
    so leaving one out keeps today's behaviour, and a wrong *yes* is the only
    mistake this could bake. The list comes from selected model metadata and
    stops at the effort trio: thinking and adaptive flags change how the binary
    builds requests and belong to models Anthropic ships.

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
    """A native registry record; capabilities must exist even when unknown.

    The runtime catalogue reads the raw literal, not zod defaults. Do not claim
    server-side advisor support for a model served by an external endpoint.
    """
    entry: dict = {
        "id": model.id,
        "family": model.id,
        "display_name": _display_name(model.label),
        "provider_ids": {"first_party": model.id},
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


def _custom_models(source: Source, options: Options, outcome: Outcome) -> Source:
    """Register and route the chosen custom models."""
    options.endpoint = endpoint(options.endpoint)
    options.custom_models = validate_models(source, options.custom_models)
    model_ids = [m.id for m in options.custom_models]
    # Classified once here for the gate and the note; the registrations below
    # re-derive from the source each hands the next, since every batch of edits
    # is a new parse. A count that moves on a green run is the early warning.
    override, general = _resolvers(source)
    outcome.note(f"resolvers: {len(override)} override, {len(general)} general")
    # Aliases are explicit configuration, so a missing resolver is a failure,
    # never permission to quietly drop a requested handle.
    shorts = aliases(options.custom_models)

    # `general-resolver` is owed exactly when a shortcut is; `context` declares
    # itself where its own work is owed (see _register_context).
    outcome.declare(
        required=(
            "enum",
            "validator",
            "resolver",
            *(("general-resolver",) if shorts else ()),
            "redirect",
            "credential",
        ),
        optional=("picker", "registry"),
    )
    source = _register(
        source,
        model_enums(source),
        model_names(options.custom_models),
        outcome.step("enum"),
    )
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
    source = _credential(source, options, outcome.step("credential"))
    source = _stateless(source, options, outcome)
    source = _classic(source, options, outcome)
    source = _effort_limits(source, options, outcome)
    source = _register_picker(source, options.custom_models, outcome.step("picker"))
    source = _register_context(source, options.custom_models, outcome)
    return _register_registry(source, options.custom_models, outcome.step("registry"))


def _from_setting(options: Options, value: object) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        raise ConfigurationError("custom_models must contain endpoint and models")
    url = value.get("endpoint", "")
    if not isinstance(url, str):
        raise ConfigurationError("endpoint must be a URL string")
    options.endpoint = endpoint(url) if url else ""
    options.custom_models = models_from(value.get("models", []))


def _setting_value(options: Options) -> dict:
    return {"endpoint": options.endpoint, "models": model_values(options.custom_models)}


def _manifest_value(options: Options) -> dict:
    """What was baked; reported window choices stay in the cache."""
    baked = [replace(model, context_options=()) for model in options.custom_models]
    return _setting_value(replace(options, custom_models=baked))


_CUSTOM_SETTING = Setting(
    key="custom_models",
    recorded=lambda o: bool(o.custom_models),
    to_manifest=_manifest_value,
    from_manifest=_from_setting,
    to_cache=_setting_value,
    from_cache=_from_setting,
)


PATCHES = [
    Patch(
        id="custom-models",
        title="Custom models",
        group=GROUP_MODELS,
        fn=_custom_models,
        default=False,
        anchors=(
            "Optional model override",
            '"opusplan"',
            f"case{_BEST}:",
            f"{_DEFAULT_BASE_URL}:",
            _MAX_CONTEXT,
            f"{_ALIASES}:",
            "prepareRequest",
            "modelHeldStateless",
            '"mid_conv_system"',
            '"context_management"',
            _CAPABILITIES,
        ),
        setting=_CUSTOM_SETTING,
    ),
]
