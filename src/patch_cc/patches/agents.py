"""Subagent patches: prompt visibility, and overriding built-in models.

Everything the model override offers is discovered from the bundle itself:

* **Agents** come from the built-in definition shape
  ``agentType:"<name>",whenToUse:...`` carrying ``source:"built-in"``.
* **Models** come from the Task tool's own input schema -- the
  ``model:enum([...])`` whose describe-string starts "Optional model override".

So a new upstream agent or model shows up here without a code change, and we
can never offer a name the binary would reject.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .base import GROUP_AGENTS, IDENT, Options, Outcome, Patch, compile_js, splice

# ------------------------------------------------------- prompt visibility

_BACKGROUNDED = '"Backgrounded agent"'
_LIVE_PROMPT_MOUNT = compile_js(
    rf"({IDENT})&&({IDENT})&&({IDENT})\.createElement\(m,\{{marginBottom:1\}},"
    rf"\3\.createElement\(({IDENT}),\{{prompt:\2\}}\)\)"
)
# The negation is required, not optional: the rewrite emits `&&!prompt`, so on a
# hypothetical un-negated shape it would invert the guard rather than relax it --
# hiding the prompt in exactly the state this step exists to fix. A build that
# ships that shape earns a branch; it must not be swept in by a loose `!?`.
_EMPTY_STATE = compile_js(
    rf"if\(({IDENT})\.length===0&&!\(({IDENT})&&({IDENT})\)\)return"
)
_TRANSCRIPT_MODE = compile_js(rf"isTranscriptMode:({IDENT})=!1")


def _subagent_prompt(content: str, _options: Options, outcome: Outcome) -> str:
    """Show the subagent ``Prompt`` block outside transcript mode."""
    gate = outcome.step("gate", expect=True)
    output = content
    index = 0

    while True:
        anchor = output.find(_BACKGROUNDED, index)
        if anchor == -1:
            break
        fn_start = output.rfind("function ", 0, anchor)
        fn_end_candidate = output.find("function ", anchor + len(_BACKGROUNDED))
        fn_end = len(output) if fn_end_candidate == -1 else fn_end_candidate
        index = anchor + len(_BACKGROUNDED)
        if fn_start == -1 or fn_end <= fn_start:
            continue

        segment = output[fn_start:fn_end]
        relevant = (
            'action:"app:toggleTranscript"' in segment
            and 'fallback:"ctrl+o"' in segment
            and "isTranscriptMode:" in segment
            and "{prompt:" in segment
            and ",theme:" in segment
        )
        if not relevant:
            continue

        transcript = _TRANSCRIPT_MODE.search(segment)
        if not transcript:
            continue
        transcript_var = transcript.group(1)
        gate_pattern = compile_js(rf"{re.escape(transcript_var)}&&({IDENT})&&")

        def drop_gate(match: re.Match[str]) -> str:
            prompt_var = match.group(1)
            nearby = segment[match.end() : match.end() + 260]
            if f"{{prompt:{prompt_var},theme:" not in nearby:
                return match.group(0)
            gate.candidates += 1
            gate.applied += 1
            return f"{prompt_var}&&"

        next_segment = gate_pattern.sub(drop_gate, segment)
        if next_segment != segment:
            output = splice(output, fn_start, fn_end, next_segment)
            index = fn_start + len(next_segment)

    mount = outcome.step("mount")

    def rewrite_mount(match: re.Match[str]) -> str:
        _transcript, prompt_var, ns, component = match.groups()
        mount.candidates += 1
        replacement = (
            f"{prompt_var}&&{ns}.createElement(m,{{marginBottom:1}},"
            f"{ns}.createElement({component},{{prompt:{prompt_var}}}))"
        )
        if replacement != match.group(0):
            mount.applied += 1
        return replacement

    output = _LIVE_PROMPT_MOUNT.sub(rewrite_mount, output)

    # Independently load-bearing: with no rows yet, the untouched guard returns
    # early and the prompt stays hidden however well the gate rewrite landed.
    empty = outcome.step("empty-state", expect=True)

    def rewrite_empty(match: re.Match[str]) -> str:
        rows, _transcript, prompt_var = match.groups()
        empty.candidates += 1
        replacement = f"if({rows}.length===0&&!{prompt_var})return"
        if replacement != match.group(0):
            empty.applied += 1
        return replacement

    return _EMPTY_STATE.sub(rewrite_empty, output)


# ----------------------------------------------------------- discovery

#: Always offered besides the discovered aliases: keep the agent on whatever
#: the main loop runs.
INHERIT = "inherit"

_AGENT_DEF = compile_js(r'agentType:"([\w-]+)",whenToUse:')
_MODEL_FIELD = compile_js(r'model:"([\w\[\].-]+)"')
#: A definition object is scanned at most this far; every known definition fits
#: well within it, and the cap keeps a moved anchor from swallowing a neighbour.
_DEF_WINDOW = 3000

_MODEL_ENUM = compile_js(
    rf'model:{IDENT}\.enum\(\[((?:"[\w\[\]]+",?)+)\]\)\.optional\(\)'
    rf'\.describe\([`"]Optional model override'
)
#: Used only if the Task-tool schema anchor ever disappears.
_FALLBACK_MODELS = ("haiku", "sonnet", "opus")


@dataclass(slots=True, frozen=True)
class BuiltinAgent:
    """One built-in agent definition as found in a bundle."""

    name: str
    #: Current ``model:"..."`` literal, or ``None`` when the definition has no
    #: model field (which the runtime treats as inherit).
    model: str | None
    #: Offset of the definition anchor in the scanned source.
    start: int
    #: Offset of the model *value* inside the source, ``-1`` when absent.
    model_start: int
    #: Where a ``model:"...",`` property would be inserted.
    insert_at: int

    @property
    def effective_model(self) -> str:
        return self.model or INHERIT


def discover_agents(source: str) -> list[BuiltinAgent]:
    """Built-in agent definitions as they exist in *this* bundle.

    Definitions marked internal (their ``whenToUse`` says so) are not offered:
    they are orchestration plumbing, not agents a user chooses.
    """
    agents: list[BuiltinAgent] = []
    seen: set[str] = set()
    for match in _AGENT_DEF.finditer(source):
        name = match.group(1)
        window = source[match.start() : match.start() + _DEF_WINDOW]
        stop = window.find("getSystemPrompt:")
        span = window if stop == -1 else window[:stop]
        if 'source:"built-in"' not in span or 'whenToUse:"Internal' in span:
            continue
        if name in seen:
            continue
        seen.add(name)
        field = _MODEL_FIELD.search(span)
        agents.append(
            BuiltinAgent(
                name=name,
                model=field.group(1) if field else None,
                start=match.start(),
                model_start=match.start() + field.start(1) if field else -1,
                insert_at=match.end() - len("whenToUse:"),
            )
        )
    return agents


def discover_models(source: str) -> list[str]:
    """Model aliases the binary's own Task tool accepts for subagents."""
    match = _MODEL_ENUM.search(source)
    if not match:
        return list(_FALLBACK_MODELS)
    return re.findall(r'"([\w\[\]]+)"', match.group(1))


# --------------------------------------------------------- model overrides

# One helper resolves a built-in agent's default model and, for exactly one
# agent (Explore today), ignores the definition's model field in favour of its
# own pin. Overriding that agent means neutralising this bypass so the
# definition -- which we just rewrote -- is authoritative again.
#
# The helper's *head* -- the two-condition guard naming the pinned agent -- is
# what identifies it, and it outlives its body: 2.1.217 grew a
# CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP escape hatch in the middle while the
# head stayed put. Resolving the pinned agent from the head alone is what keeps
# a reshaped body loud: we still know an override is at stake, so the failed
# rewrite is reported instead of vanishing with the agent's identity.
_MODEL_BYPASS_HEAD = compile_js(
    rf"function {IDENT}\(({IDENT}),{IDENT}\)\{{"
    rf'if\(\1\.agentType!==({IDENT})\.agentType\|\|\1\.source!=="built-in"\)'
    rf"(?:\{{return \1\.model\}}|return \1\.model;)"
)

# The whole helper, replaced wholesale. Upstream's guards between head and the
# pin-or-inherit return are matched as opaque brace-free statements rather than
# earning a branch each.
_MODEL_BYPASS = compile_js(
    rf"function ({IDENT})\(({IDENT}),({IDENT})\)\{{"
    rf'if\(\2\.agentType!==({IDENT})\.agentType\|\|\2\.source!=="built-in"\)'
    rf"(?:\{{return \2\.model\}}|return \2\.model;)"
    rf'[^{{}}]{{0,300}}?return {IDENT}\(\3\)\?{IDENT}:"inherit"\}}'
)


def bypassed_agents(source: str) -> list[tuple[str, int]]:
    """Every pinned agent a bypass helper overrides, with the helper's offset.

    Carrying the offset is what keeps identification and rewriting talking about
    the *same* helper: matching the head here and then searching for a body
    somewhere else could neutralise an unrelated helper and call it done.
    """
    found = []
    for match in _MODEL_BYPASS_HEAD.finditer(source):
        def_var = re.escape(match.group(2))
        assign = compile_js(rf'(?<![\w$]){def_var}=\{{agentType:"([\w-]+)"').search(
            source
        )
        if assign:
            found.append((assign.group(1), match.start()))
    return found


def _neutralize_bypass(content: str, at: int, step: Outcome) -> str:
    """Rewrite the bypass helper that starts at ``at`` so it obeys the definition.

    The candidate is counted before the body is matched: the head already
    proved a helper is here, so a body that fails to match is a drift to report
    (``matched 1 but rewrote none``), not the absence the count would otherwise
    claim.
    """
    step.candidates += 1
    match = _MODEL_BYPASS.match(content, at)
    if not match:
        step.note(
            "bypass helper found but its body drifted; the pinned agent would "
            "ignore its override"
        )
        return content
    step.applied += 1
    name, obj, model = match.group(1, 2, 3)
    return splice(
        content,
        match.start(),
        match.end(),
        f"function {name}({obj},{model}){{return {obj}.model}}",
    )


def _subagent_models(content: str, options: Options, outcome: Outcome) -> str:
    """Write the chosen model into each overridden built-in definition.

    Definitions with a ``model:"..."`` literal get it rewritten; definitions
    without one get it inserted. Both target offsets from a fresh discovery
    pass, so this never guesses about the bytes between anchor and value.
    """
    if not options.subagent_models:
        outcome.note("no subagent model overrides configured")
        return content

    output = content
    offered = {INHERIT, *discover_models(output)}

    for agent, target in sorted(options.subagent_models.items()):
        step = outcome.step(agent)
        if target not in offered:
            step.note(f"model {target!r} not offered by this bundle; skipped")
            continue
        info = next((a for a in discover_agents(output) if a.name == agent), None)
        if info is None:
            step.note(f"no built-in agent {agent!r} in this bundle; skipped")
            continue

        step.candidates += 1
        if info.effective_model == target:
            continue  # already the desired model
        if info.model is None:
            output = splice(
                output, info.insert_at, info.insert_at, f'model:"{target}",'
            )
        else:
            output = splice(
                output, info.model_start, info.model_start + len(info.model), target
            )
        step.applied += 1

    # Re-resolved after the definition rewrites above, so the offsets are live.
    # Later helpers first: neutralising one changes the length of the source.
    pinned_agents = bypassed_agents(output)
    if not pinned_agents:
        # No helper found means either upstream stopped pinning an agent or the
        # head drifted -- and we cannot tell which, because the head is what
        # names the agent. A drifted *body* is loud (the step below fails); this
        # is the same failure one level up, where there is no step to fail, so
        # the note is the only thing standing between a dead override and a
        # green tick. It prints on every run, healthy ones included.
        outcome.note(
            "no model-bypass helper found; if this build still pins an agent, "
            "its override is inert"
        )
    for pinned, at in reversed(pinned_agents):
        if pinned in options.subagent_models:
            step = outcome.step(f"bypass:{pinned}", expect=True)
            output = _neutralize_bypass(output, at, step)

    return output


PATCHES = [
    Patch(
        id="subagent-prompt",
        title="Show subagent prompts",
        summary="Show a subagent's Prompt block during normal use, not only in transcript mode.",
        group=GROUP_AGENTS,
        fn=_subagent_prompt,
        anchors=('"Backgrounded agent"', 'action:"app:toggleTranscript"'),
    ),
    Patch(
        id="subagent-models",
        title="Override subagent models",
        summary="Choose the default model for the built-in agents found in your binary.",
        group=GROUP_AGENTS,
        fn=_subagent_models,
        default=False,
        anchors=('agentType:"', "Optional model override"),
    ),
]
