"""Subagent patches: prompt visibility, and overriding built-in models.

Everything the model override offers is discovered from the bundle itself:

* **Agents** come from the built-in definition shape -- an object carrying
  ``agentType``, ``whenToUse`` and ``source:"built-in"``.
* **Models** come from the Task tool's own input schema -- the ``model``
  property whose describe-string starts "Optional model override".

So a new upstream agent or model shows up here without a code change, and we
can never offer a name the binary would reject.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import js
from ..js import Edit, Source
from .base import GROUP_MODELS, GROUP_OUTPUT, Options, Outcome, Patch, Setting

# ------------------------------------------------------- prompt visibility

_BACKGROUNDED = '"Backgrounded agent"'
_TRANSCRIPT_MODE = "isTranscriptMode"
_PROMPT = "prompt"


def _prompt_vars(scope: js.Node) -> set[str]:
    """The variables this component hands to a renderer as its ``prompt``.

    What makes a value *the subagent's prompt* is that the component passes it
    under that name -- not how it was obtained, which upstream spells as a
    destructured parameter in one component and a ternary over ``.prompt`` in
    the other, and not how it is defended on the way there. Every identifier
    *inside* the value counts, because requiring the value to be a bare
    identifier reads a single ``??""`` as the prompt no longer existing: the one
    ``prompt:h`` pair in the progress-messages component feeds two of the four
    gates, so ``prompt:h??""`` would quietly take that component's Prompt block
    and its empty state with it, and the patch would stay green at 2/4.
    """
    found: set[str] = set()
    for node in js.every(
        scope, lambda n: n.type == "property_identifier" and js.text(n) == _PROMPT
    ):
        pair = js.named(node)
        if pair is None:
            continue
        value = pair.child_by_field_name("value")
        if value is not None:
            found.update(js.text(n) for n in js.every(value, js.of_type("identifier")))
    return found


def _rebound(name: str, site: js.Node, owner: js.Node) -> bool:
    """Is this name a *different* variable here than the one ``owner`` binds?

    Minified locals are single letters and they repeat constantly, so a name on
    its own is not a variable -- it is a spelling, and the scope it was read in
    is the rest of it. A callback inside the component taking its own ``b``
    would read as the component's ``b``, and the conjunction rewritten would be
    the callback's own: both halves matched by spelling, the count up by one,
    the patch green.

    So the scopes between the use and the component are asked whether any of
    them binds it, which is the question a resolver would answer and the only
    part of one this needs.
    """
    scope = js.climb(site, lambda n: n.type in js.FUNCTIONS)
    while scope is not None and scope.start_byte != owner.start_byte:
        bound = {js.text(js.binding(p)) for p in js.positional(scope)}
        bound |= {js.text(b) for b in js.parameters(scope).values()}
        bound |= {
            js.text(local)
            for declarator in js.every(
                js.body(scope), js.of_type("variable_declarator"), scoped=True
            )
            for local in js.every(
                declarator.child_by_field_name("name"), js.of_type("identifier")
            )
        }
        if name in bound:
            return True
        scope = js.climb(scope.parent, lambda n: n.type in js.FUNCTIONS)
    return False


def _subagent_prompt(source: Source, _options: Options, outcome: Outcome) -> Source:
    """Show the subagent ``Prompt`` block outside transcript mode.

    Every gate is the same conjunction -- *in transcript mode, and there is a
    prompt* -- and dropping the first half is the whole patch. Four gates spell
    it on every build in the corpus: the block's mount, a second mount in the
    completed state, an early return that renders an empty state unless the
    prompt is showing, and the prompt render in the progress-messages
    component. They differ only in what surrounds them, and one rule reaches
    all four.

    Both halves are named by upstream: the transcript flag is a parameter
    property, and the prompt is whatever the component passes as ``prompt``.
    That pairing is what keeps the rewrite off the neighbouring
    ``transcript && content && ...`` conjunction, which gates the agent's
    *output* and is not this patch's business.
    """
    outcome.declare(required=("gate",))
    gate = outcome.step("gate")
    edits: list[Edit] = []
    seen: set[int] = set()

    for node in source.find(_TRANSCRIPT_MODE):
        component = js.climb(
            node,
            lambda n: n.type in js.FUNCTIONS and _TRANSCRIPT_MODE in js.parameters(n),
        )
        if component is None or component.id in seen:
            continue
        seen.add(component.id)
        transcript = js.text(js.parameters(component)[_TRANSCRIPT_MODE])
        prompts = _prompt_vars(component)
        if not prompts:
            continue
        for conjunction in js.every(component, js.of_type("binary_expression")):
            pair = js.conjuncts(conjunction)
            if pair is None:
                continue
            # One half is the flag, the other is the prompt, and the patch keeps
            # the prompt. Which of the two upstream writes first is the
            # minifier's business as much as anything else here: asking for the
            # flag on the left would cost a whole Prompt block the day one gate
            # is spelled the other way round, at 3/4 and green.
            showing = [side for side in pair if js.text(side) in prompts]
            if len(showing) != 1 or not any(
                js.text(side) == transcript for side in pair
            ):
                continue
            kept = showing[0]
            if any(_rebound(js.text(side), conjunction, component) for side in pair):
                continue
            gate.candidates += 1
            gate.applied += 1
            edits.append(Edit.replace(conjunction, js.text(kept)))

    return source.apply(edits)


# ----------------------------------------------------------- discovery

#: Always offered besides the discovered aliases: keep the agent on whatever
#: the main loop runs.
INHERIT = "inherit"

_AGENT_TYPE = "agentType"
_WHEN_TO_USE = "whenToUse"
_SOURCE = "source"
_BUILT_IN = '"built-in"'
_MODEL = "model"

#: The Task tool's own describe-string. The enum it introduces is the list of
#: aliases a subagent may be pinned to, and one home for it matters: `enum`
#: (in `custom-models`) splices imported ids into the very array
#: :func:`discover_models` reads back out, and patches/__init__.py explains why
#: that ordering is load-bearing. Matched separately, the two would drift apart
#: on the next upstream reshape and only one of them would be repaired.
MODEL_DESCRIPTION = "Optional model override"


@dataclass(slots=True, frozen=True)
class BuiltinAgent:
    """One built-in agent definition as found in a bundle."""

    name: str
    #: Current ``model`` literal, or ``None`` when the definition has none
    #: (which the runtime treats as inherit).
    model: str | None
    #: The ``model`` value node, when the definition carries one.
    model_node: js.Node | None
    #: The ``whenToUse`` property, before which a ``model`` property is
    #: inserted for a definition that has none. Any property boundary would do
    #: -- an object literal takes a new property before any existing one -- and
    #: this one is required to be present for the definition to count at all.
    anchor: js.Node
    #: The variable this definition is assigned to, when it is assigned to one.
    #: The model-bypass helper names its pinned agent that way.
    holder: str | None

    @property
    def effective_model(self) -> str:
        return self.model or INHERIT


def _holder(definition: js.Node) -> str | None:
    """The variable a definition object is assigned to, if any."""
    parent = definition.parent
    if parent is None:
        return None
    if parent.type == "assignment_expression":
        left = parent.child_by_field_name("left")
        return js.text(left) if left is not None and left.type == "identifier" else None
    if parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
        return js.text(name) if name is not None and name.type == "identifier" else None
    return None


def _resolved_name(source: Source, value: js.Node) -> str | None:
    """The agent name an ``agentType`` value spells -- through its value node.

    A string literal is its own name. An identifier is a *hoisted constant*
    (``agentType:Ehr`` with ``Ehr="worker"`` declared elsewhere -- upstream does
    this to 4 of 11 built-ins on 2.1.233, ``fork``/``claude-code-guide``/
    ``web-fetch``/``worker``), and it is resolved to the single declarator that
    binds it to a string. That is the mirror of :func:`_holder`, which walks the
    other way -- a definition to the variable holding it -- and it is what makes
    the skip an honest one: a name read through its value is a name we can still
    *write*, and ``name`` is only used to offer the agent (the edit hangs off
    ``anchor``/``model_node``), so nothing about the rewrite changes.

    The declarator is looked for in the constant's *own module*
    (:meth:`js.Source.find_local`): a minified local is only unique there, and
    since 2.1.242 the bundle is many modules, so the same two letters bind a
    string in a dozen unrelated chunks. Before the split the module was the whole
    bundle and the distinction did not exist.

    ``None`` is ordinary absence -- a computed or otherwise unreadable type, the
    answer a literal-only read gives every constant. Two declarators for one
    identifier is not absence but ambiguity, and :func:`js.only` makes it loud
    rather than picking one, the same cardinality rule every rewrite here
    follows.
    """
    if value.type == "string":
        return js.text(value)[1:-1]
    if value.type != "identifier":
        return None
    name = js.text(value)
    binding = js.only(
        [
            declarator
            for node in source.find_local(value, name)
            if node.type == "identifier"
            and (declarator := node.parent) is not None
            and declarator.type == "variable_declarator"
            and declarator.child_by_field_name("name") == node
            and (bound := declarator.child_by_field_name("value")) is not None
            and bound.type == "string"
        ],
        f"declarations binding {name!r} to a string",
    )
    if binding is None:
        return None
    return js.text(binding.child_by_field_name("value"))[1:-1]


def discover_agents(source: Source) -> list[BuiltinAgent]:
    """Built-in agent definitions as they exist in *this* bundle.

    A definition is an object carrying all three of the properties that make
    one, and nothing is said about their order or about how much text sits
    between them: an object has an end, where a scan window has to guess one.

    Definitions marked internal (their ``whenToUse`` says so) are not offered:
    they are orchestration plumbing, not agents a user chooses.
    """
    agents: list[BuiltinAgent] = []
    seen: set[str] = set()
    for node in source.find(_AGENT_TYPE):
        pair = js.named(node)
        if pair is None:
            continue
        definition = js.owner(pair)
        if definition is None:
            continue
        carried = js.props(definition)
        kind, when, origin = (
            carried.get(_AGENT_TYPE),
            carried.get(_WHEN_TO_USE),
            carried.get(_SOURCE),
        )
        if when is None or origin is None or js.text(origin) != _BUILT_IN:
            continue
        # The type is a string literal on most definitions and a hoisted constant
        # on the rest; :func:`_resolved_name` reads it through its value either
        # way, so a name upstream lifted into a variable is still offered rather
        # than silently dropped. Only a type that resolves to no single string --
        # genuinely unreadable, and so unwritable -- is skipped.
        name = _resolved_name(source, kind) if kind is not None else None
        if name is None:
            continue
        if when.type == "string" and js.text(when)[1:-1].startswith("Internal"):
            continue
        if name in seen:
            continue
        seen.add(name)
        model = carried.get(_MODEL)
        agents.append(
            BuiltinAgent(
                name=name,
                model=js.text(model)[1:-1] if model is not None else None,
                model_node=model,
                anchor=js.up(when, "pair") or when,
                holder=_holder(definition),
            )
        )
    return agents


def model_enums(source: Source) -> list[js.Node]:
    """The Task tool's ``model`` enum arrays -- the aliases a subagent accepts.

    Reached from the describe-string through the property it belongs to, so
    whatever expression this build uses to build the schema is skipped without
    being described. That callee is pure minifier noise: one unchanged
    ``effortLevel`` schema read ``E.enum(``, ``A.enum(``, ``b.enum(``,
    ``v.enum(``, ``w.enum(`` and then ``Ir(`` across eight builds, and every
    matcher that named any of it dated itself to one of them.

    Skipping that callee unmodelled is exactly what makes the array worth
    naming by its contents. The value is whatever expression this build reached
    the factory through, and "the first array in it" is a guess about that
    expression -- the one kind of claim this module exists to stop making. So
    the arrays under the value are candidates and membership picks between
    them: an enum of aliases is an array of nothing but string literals. That
    is the only membership this site can assert, the names being precisely what
    it exists to discover, and it is enough to tell the enum from an array of
    schemas, options or tuples composed alongside it.

    Every enum so described, not the first one: the anchor is a *sentence*, and
    nothing stops a build from introducing a second tool with it. "Which array
    did upstream mean" would then have no answer, while the question actually
    asked here -- every list of aliases a subagent may be pinned to -- has the
    same one either way, both to read back out and to register a new id in.
    """
    found = []
    for described in source.literals(MODEL_DESCRIPTION):
        pair = js.up(described, "pair")
        if pair is None:
            continue
        key = pair.child_by_field_name("key")
        if key is None or js.text(key) != _MODEL:
            continue
        found += [
            enum
            for enum in js.every(pair.child_by_field_name("value"), js.of_type("array"))
            if (aliases := js.elements(enum))
            and all(alias.type == "string" for alias in aliases)
        ]
    return found


def discover_models(source: Source) -> list[str]:
    """Model aliases the binary's own Task tool accepts for subagents.

    Empty when the Task-tool schema anchor is gone, and deliberately not backed
    by a guessed default. A hardcoded fallback (`haiku/sonnet/opus`) would read
    a lost enum as those three: `doctor` prints a plausible list, every pin to
    one of them lands, and a pin to a name the real bundle accepts is refused --
    the same emptiness that `custom-models`' own required `enum` step reports
    as broken in the same run. With nothing to offer, the offer is empty:
    `discover_models` returns `[]`, `doctor` shows `models: inherit` alone, and a
    requested pin fails its required step rather than landing on a name nothing
    registered. Absence flows through the path a present-but-different name does.
    """
    return list(
        dict.fromkeys(
            alias for enum in model_enums(source) for alias in js.strings(enum)
        )
    )


# --------------------------------------------------------- model overrides

_INHERIT_LITERAL = '"inherit"'


@dataclass(slots=True, frozen=True)
class Bypass:
    """A helper that ignores one agent's definition in favour of its own pin."""

    agent: str
    body: js.Node
    definition: str


def _origin_test(literal: js.Node) -> str | None:
    """Whose origin a ``!=="built-in"`` test asks about -- the definition it reads.

    Both operands are nodes and the comparison is one, so which side the string
    sits on says nothing about what is being tested.
    """
    test = js.up(literal, "binary_expression")
    operator = test.child_by_field_name("operator") if test is not None else None
    if test is None or operator is None or js.text(operator) != "!==":
        return None
    sides = (test.child_by_field_name("left"), test.child_by_field_name("right"))
    read = next((side for side in sides if js.reads(side, _SOURCE)), None)
    return None if read is None or literal not in sides else js.text(js.receiver(read))


def _pinned_agent(condition: js.Node | None, subject: str) -> str | None:
    """The *other* definition the guard names -- the agent this helper pins.

    The guard compares the definition it was handed (``subject``) against a
    second one, and that second one is the pin. Which is why it is found by
    *not* being the subject: both are read as ``<local>.agentType``, and the
    locals are the minifier's.

    A guard naming two others pins two agents, which this cannot express and so
    refuses rather than picks from.
    """
    return js.only(
        sorted(
            {
                js.text(js.receiver(read))
                for read in js.every(condition, lambda n: js.reads(n, _AGENT_TYPE))
                if js.text(js.receiver(read)) != subject
            }
        ),
        "agents this bypass guard pins",
    )


def bypassed_agents(source: Source, agents: list[BuiltinAgent]) -> list[Bypass]:
    """Every pinned agent a bypass helper overrides, with the body to replace.

    One helper resolves a built-in agent's default model and, for exactly one
    agent (Explore today), ignores the definition's model field in favour of
    its own pin. Overriding that agent means neutralising this helper so the
    definition -- which we just rewrote -- is authoritative again.

    Its *guard* is what identifies it and names the pinned agent, and it
    outlives its body: 2.1.217 grew a
    ``CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP`` escape hatch in the middle
    while the guard stayed put, so a matcher that spells the body silently
    loses every Explore override there, where the body taken as a node has
    nothing to skip.

    The guard is read as the comparison it is. Spelled as the phrase
    ``.source!=="built-in"`` it would also assert which side upstream writes
    the string on, and ``"built-in"!==e.source`` -- one reordering, the same
    test -- would read as no helper being there at all.

    Which agent a local stands for is likewise a question with one right
    answer or none: two definitions held under one minified name are two
    answers, and taking the first would map the bypass to the wrong agent,
    leave the real override inert, and fire no note because a bypass *was*
    found.
    """
    holders: dict[str, list[BuiltinAgent]] = {}
    for agent in agents:
        if agent.holder:
            holders.setdefault(agent.holder, []).append(agent)

    found: list[Bypass] = []
    for literal in source.find(_BUILT_IN):
        subject = _origin_test(literal)
        helper = js.climb(literal, lambda n: n.type in js.FUNCTIONS)
        block = js.body(helper)
        guard = js.up(literal, "if_statement")
        if subject is None or block is None or guard is None or guard.parent != block:
            continue
        pinned = _pinned_agent(guard.child_by_field_name("condition"), subject)
        if pinned is None:
            continue
        held = js.only(holders.get(pinned, []), f"definitions held by {pinned}")
        if held is None:
            continue
        found.append(Bypass(agent=held.name, body=block, definition=subject))
    return found


def _subagent_models(source: Source, options: Options, outcome: Outcome) -> Source:
    """Write the chosen model into each overridden built-in definition.

    Definitions with a ``model`` literal get it rewritten; definitions without
    one get it inserted before a property they are required to carry, so
    nothing here has to find where the object *ends*.
    """
    if not options.subagent_models:
        outcome.note("no subagent model overrides configured")
        return source

    # The bundle in hand is the only authority on what a subagent may be pinned
    # to -- including imported custom ids, which custom-models has already
    # written into this very enum by the time we run (see patches/__init__.py).
    # Asking the bundle rather than the options is what ties a pin to its
    # registration: a custom-models the fixpoint dropped leaves no alias here,
    # so the pin fails with it instead of landing on a model nothing registered.
    offered = {INHERIT, *discover_models(source)}
    agents = discover_agents(source)
    found = {agent.name: agent for agent in agents}
    edits: list[Edit] = []

    # Required: every override reaching a patch has already been validated
    # against this bundle by its surface (`apply` or the menu), so
    # one that cannot be written is not a shape this build lacks -- it is the
    # asked-for change failing. Left optional, the patch would stay green, the
    # binary ship without the override, and the manifest claim it.
    outcome.declare(required=tuple(sorted(options.subagent_models)))
    for name, target in sorted(options.subagent_models.items()):
        step = outcome.step(name)
        if target not in offered:
            step.note(f"model {target!r} not offered by this bundle; skipped")
            continue
        agent = found.get(name)
        if agent is None:
            step.note(f"no built-in agent {name!r} in this bundle; skipped")
            continue

        step.candidates += 1
        step.applied += 1
        if agent.effective_model == target:
            # Already the desired model. Credited as landed because the step is
            # judged on what it achieved, not on whether bytes moved.
            continue
        if agent.model_node is None:
            edits.append(Edit.before(agent.anchor, f'{_MODEL}:"{target}",'))
        else:
            edits.append(Edit.replace(agent.model_node, f'"{target}"'))

    bypasses = bypassed_agents(source, agents)
    if not bypasses:
        # No helper found means either upstream stopped pinning an agent or the
        # guard drifted -- and we cannot tell which, because the guard is what
        # names the agent. A drifted *body* is loud (the step below fails); this
        # is the same failure one level up, where there is no step to fail, so
        # the note is the only thing standing between a dead override and a
        # green tick. It prints on every run, healthy ones included.
        outcome.note(
            "no model-bypass helper found; if this build still pins an agent, "
            "its override is inert"
        )
    for bypass in bypasses:
        if bypass.agent not in options.subagent_models:
            continue
        # Declared the moment the guard resolves the agent's name -- the
        # earliest this step *can* exist -- while a vanished guard stays the
        # note above, with no name left to declare.
        outcome.declare(required=(f"bypass:{bypass.agent}",))
        step = outcome.step(f"bypass:{bypass.agent}")
        step.candidates += 1
        step.applied += 1
        edits.append(
            Edit.replace(bypass.body, f"{{return {bypass.definition}.{_MODEL}}}")
        )

    return source.apply(edits)


def _pins_from(value: object) -> dict[str, str]:
    """A subagent-pin map, keeping only the ``str -> str`` pairs a store held."""
    if not isinstance(value, dict):
        return {}
    return {a: m for a, m in value.items() if isinstance(a, str) and isinstance(m, str)}


def _set_pins(options: Options, value: object) -> None:
    options.subagent_models = _pins_from(value)


_SUBAGENT_SETTING = Setting(
    key="subagent_models",
    recorded=lambda o: bool(o.subagent_models),
    to_manifest=lambda o: o.subagent_models,
    from_manifest=_set_pins,
    to_cache=lambda o: o.subagent_models,
    from_cache=_set_pins,
)


PATCHES = [
    Patch(
        id="subagent-prompt",
        title="Show subagent prompts",
        group=GROUP_OUTPUT,
        fn=_subagent_prompt,
        anchors=(_BACKGROUNDED, f"{_TRANSCRIPT_MODE}:", f"{_PROMPT}:"),
    ),
    Patch(
        id="subagent-models",
        title="Override subagent models",
        group=GROUP_MODELS,
        fn=_subagent_models,
        default=False,
        anchors=(f"{_AGENT_TYPE}:", MODEL_DESCRIPTION, _INHERIT_LITERAL),
        setting=_SUBAGENT_SETTING,
    ),
]
