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
_BUILT_IN = "built-in"
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
    #: What the definition runs untouched, for display only.
    default: str
    #: The ``model`` entry however it is spelled, when the definition carries one.
    entry: js.Node | None
    #: The ``whenToUse`` property, before which a ``model`` property is
    #: inserted for a definition that has none. Any property boundary would do
    #: -- an object literal takes a new property before any existing one -- and
    #: this one is required to be present for the definition to count at all.
    anchor: js.Node
    definition: js.Node


def _spelled(source: Source, node: js.Node | None) -> str | None:
    """The one string ``node`` can stand for, else ``None``."""
    spelled = {
        js.text(value)[1:-1] if value.type == "string" else None
        for value in source.values(node)
    }
    return spelled.pop() if len(spelled) == 1 else None


def _default(source: Source, model: js.Node | None) -> str:
    """What a ``model`` value runs; an absent one is ``undefined``, run as inherit."""
    shown = (
        js.text(value)[1:-1] if value.type == "string" else "computed"
        for value in source.values(model)
    )
    return "/".join(dict.fromkeys(shown)) or INHERIT


def _definitions(source: Source) -> list[tuple[js.Node, dict[str, js.Node]]]:
    """Every built-in agent definition a user may choose, with its properties.

    A definition is an object carrying all three of the properties that make
    one, and nothing is said about their order or about how much text sits
    between them: an object has an end, where a scan window has to guess one.

    Definitions marked internal (their ``whenToUse`` says so) are left out:
    they are orchestration plumbing, not agents a user chooses.
    """
    found = []
    for node in source.find(_AGENT_TYPE):
        definition = js.owner(js.named(node))
        carried = js.props(definition)
        when = carried.get(_WHEN_TO_USE)
        if (
            definition is not None
            and when is not None
            and _spelled(source, carried.get(_SOURCE)) == _BUILT_IN
            and not (_spelled(source, when) or "").startswith("Internal")
        ):
            found.append((definition, carried))
    return found


def discover_agents(source: Source) -> list[BuiltinAgent]:
    """Built-in agent definitions as they exist in *this* bundle, by name."""
    agents: dict[str, BuiltinAgent] = {}
    for definition, carried in _definitions(source):
        name = _spelled(source, carried.get(_AGENT_TYPE))
        if name is None or name in agents:
            continue
        model = carried.get(_MODEL)
        agents[name] = BuiltinAgent(
            name=name,
            default=_default(source, model),
            entry=None if model is None else js.entry(model),
            anchor=js.entry(carried[_WHEN_TO_USE]),
            definition=definition,
        )
    return list(agents.values())


def model_enums(source: Source) -> list[list[js.Node]]:
    """The Task tool's ``model`` enum arrays, one list per schema.

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
        key = None if pair is None else pair.child_by_field_name("key")
        if pair is None or key is None or js.text(key) != _MODEL:
            continue
        enums = {
            enum.id: enum
            for enum in source.walk(pair.child_by_field_name("value"))
            if enum.type == "array"
            and (aliases := js.elements(enum))
            and all(alias.type == "string" for alias in aliases)
        }
        found.append(list(enums.values()))
    return found


def discover_models(source: Source) -> list[str]:
    """Model aliases the binary's own Task tool accepts for subagents.

    Empty when the Task-tool schema anchor is gone, and deliberately not backed
    by a guessed default. A hardcoded fallback (`haiku/sonnet/opus`) would read
    a lost enum as those three: `doctor` prints a plausible list, every pin to
    one of them lands, and a pin to a name the real bundle accepts is refused.
    With nothing to offer, the offer is empty: `discover_models` returns `[]`,
    `doctor` shows `models: inherit` alone and fails on the schema it could not
    read (:func:`undiscovered`), and no pin lands on a name nothing registered.
    Absence flows through the path a present-but-different name does.
    """
    return list(
        dict.fromkeys(
            alias
            for enums in model_enums(source)
            for enum in enums
            for alias in js.strings(enum)
        )
    )


def undiscovered(source: Source) -> list[str]:
    """What discovery found and could not read, as the menu's shortfall.

    No rewrite counts it: an unread definition is an agent nobody can pin, and
    an unread schema a list no pin is checked against. `doctor` fails on it.
    """
    missed = []
    offered, found = len(discover_agents(source)), len(_definitions(source))
    if offered < found:
        missed.append(f"agents: {offered} of {found} built-in definitions offered")
    schemas = model_enums(source)
    if not schemas or not all(schemas):
        read = sum(map(bool, schemas))
        missed.append(f"models: {read} of {len(schemas)} Task schemas read")
    return missed


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


def _pinned_agent(
    source: Source, condition: js.Node | None, subject: str
) -> js.Node | None:
    """The *other* definition the guard names -- the agent this helper pins.

    The guard compares the definition it was handed (``subject``) against a
    second one, and that second one is the pin. Which is why it is found by
    *not* being the subject: both are read as ``<local>.agentType``, and the
    locals are the minifier's.

    A guard naming two others pins two agents, which this cannot express and so
    refuses rather than picks from.
    """
    pinned = {
        value.id: value
        for read in js.every(condition, lambda n: js.reads(n, _AGENT_TYPE))
        if js.text(receiver := js.receiver(read)) != subject
        for value in source.values(receiver)
    }
    return js.only(list(pinned.values()), "agents this bypass guard pins")


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
    answer or none: a name that can hold two definitions is two answers, and
    taking the first would map the bypass to the wrong agent, leave the real
    override inert, and fire no note because a bypass *was* found.
    """
    by_definition = {agent.definition.id: agent for agent in agents}
    found: list[Bypass] = []
    for literal in source.find(f'"{_BUILT_IN}"'):
        subject = _origin_test(literal)
        helper = js.climb(literal, lambda n: n.type in js.FUNCTIONS)
        block = js.body(helper)
        guard = js.up(literal, "if_statement")
        if subject is None or block is None or guard is None or guard.parent != block:
            continue
        pinned = _pinned_agent(source, guard.child_by_field_name("condition"), subject)
        agent = None if pinned is None else by_definition.get(pinned.id)
        if agent is None:
            continue
        found.append(Bypass(agent=agent.name, body=block, definition=subject))
    return found


def _subagent_models(source: Source, options: Options, outcome: Outcome) -> Source:
    """Write the chosen model into each overridden built-in definition.

    A ``model`` entry is replaced whole, however upstream spells it -- 2.1.300
    computes web-fetch's in a getter; definitions without one get it inserted
    before a property they are required to carry, so nothing here has to find
    where the object *ends*.
    """
    if not options.subagent_models:
        outcome.note("no subagent model overrides configured")
        return source

    # The bundle in hand is the only authority on what a subagent may be pinned
    # to -- including imported custom ids, which custom-models has already
    # written into this very enum by the time we run (see patches/__init__.py).
    # Asking the bundle rather than the options is what ties a pin to its
    # registration: a custom-models the fixpoint dropped leaves no alias here,
    # so the pin is skipped with it instead of landing on a model nothing
    # registered.
    offered = {INHERIT, *discover_models(source)}
    agents = discover_agents(source)
    found = {agent.name: agent for agent in agents}
    edits: list[Edit] = []

    written: dict[str, str] = {}
    for name, target in sorted(options.subagent_models.items()):
        pin = f"{name}={target}"
        outcome.declare(owed=(pin,))
        agent = found.get(name)
        if agent is None or target not in offered:
            continue

        outcome.declare(required=(pin,))
        step = outcome.step(pin)
        step.candidates += 1
        step.applied += 1
        if agent.entry is None:
            edits.append(Edit.before(agent.anchor, f'{_MODEL}:"{target}",'))
        else:
            edits.append(Edit.replace(agent.entry, f'{_MODEL}:"{target}"'))
        written[name] = target
    # The manifest records these options: what was written, never what was asked.
    options.subagent_models = written

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
        if bypass.agent not in written:
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
