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
"""

from __future__ import annotations

import re

from .base import (
    GROUP_THINKING,
    IDENT,
    Options,
    Outcome,
    Patch,
    compile_js,
    splice,
    switch_case_end,
)

# The group-builder branch pair that decides where a finished thinking message
# goes. Branch one (queued prompts) is matched only to *discover* the flush
# function, visible list, and loop variable; it is reconstructed verbatim.
# Branch two is the swallow: stash the text as the group's summary line and
# hide the message inside the group. Rewritten to flush the open group and
# push the message into the visible flow instead, keeping the think-time
# accounting.
_GROUP_ROUTING = compile_js(
    rf"else if\(({IDENT})\(({IDENT})\)\)({IDENT})\(\),({IDENT})\.push\(\2\);"
    rf"else if\(({IDENT})!==void 0\)\{{"
    rf'if\(({IDENT})\.latestThinkingSummary=\5\.text\.trim\(\)\.replace\(/\\s\+/g," "\),'
    rf"({IDENT})!==void 0\)\{{"
    rf"let ({IDENT})=Date\.parse\(\2\.timestamp\)-Date\.parse\(\7\);"
    rf"if\(Number\.isFinite\(\8\)&&\8>0\)\6\.thoughtForMs\+=Math\.min\(\8,({IDENT}|\d+)\)\}}"
    rf"\6\.messages\.push\(\5\.message\)\}}"
)

# The early return that hides thinking outside transcript/verbose mode.
# 2.1.216 wrapped the body in braces (`{return null}`); older builds used the
# bare statement. Accept both -- missing the block form is exactly how this
# patch silently broke once.
_NULL_GUARD = compile_js(
    rf"if\(!{IDENT}(?:&&!{IDENT}){{1,2}}\)(?:\{{return null\}}|return null;?)"
)
_RENDERER_PROPS = compile_js(rf"((?:createElement|jsx|jsxs)\({IDENT},\{{)([^}}]*)\}}")
_IS_TRANSCRIPT = compile_js(r"isTranscriptMode:[^,}]+")
_HIDE_IN_TRANSCRIPT = compile_js(r"hideInTranscript:[^,}]+")


def _reroute_grouping(content: str, outcome: Outcome) -> str:
    """Send finished thinking messages to the visible list, not the group."""
    step = outcome.step("group-routing")

    def rewrite(match: re.Match[str]) -> str:
        gate, loop, flush, visible, summary, group, stamp, delta, cap = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"else if({gate}({loop})){flush}(),{visible}.push({loop});"
            f"else if({summary}!==void 0){{"
            f"if({stamp}!==void 0){{"
            f"let {delta}=Date.parse({loop}.timestamp)-Date.parse({stamp});"
            f"if(Number.isFinite({delta})&&{delta}>0)"
            f"{group}.thoughtForMs+=Math.min({delta},{cap})}}"
            f"{flush}(),{visible}.push({loop})}}"
        )

    return _GROUP_ROUTING.sub(rewrite, content)


def _thinking_inline(content: str, _options: Options, outcome: Outcome) -> str:
    """Route thinking past the collapse machinery and render it expanded.

    One rewrite in the activity-group builder (see :data:`_GROUP_ROUTING`),
    then two inside every ``case"thinking":`` arm that renders with an
    ``isTranscriptMode:`` prop: remove the early null-return, and force the
    renderer into transcript presentation (full markdown instead of the
    one-line collapsed form).
    """
    guard = outcome.step("null-guard")
    props = outcome.step("renderer-props")
    output = _reroute_grouping(content, outcome)
    needle = 'case"thinking":'
    index = 0

    while True:
        start = output.find(needle, index)
        if start == -1:
            break
        end = switch_case_end(output, start + len(needle))
        segment = output[start:end]
        index = start + len(needle)

        if "isTranscriptMode:" not in segment:
            continue

        next_segment, dropped = _NULL_GUARD.subn("", segment, count=1)
        if dropped:
            guard.candidates += 1
            guard.applied += 1

        def rewrite_props(match: re.Match[str]) -> str:
            prefix, body = match.group(1), match.group(2)
            updated = _IS_TRANSCRIPT.sub("isTranscriptMode:!0", body)
            updated = _HIDE_IN_TRANSCRIPT.sub("hideInTranscript:!1", updated)
            if updated == body:
                return match.group(0)
            props.candidates += 1
            props.applied += 1
            return f"{prefix}{updated}}}"

        next_segment = _RENDERER_PROPS.sub(rewrite_props, next_segment)

        if next_segment != segment:
            output = splice(output, start, end, next_segment)
            index = start + len(next_segment)

    return output


PATCHES = [
    Patch(
        id="thinking-inline",
        title="Always show thinking",
        summary="Render thinking blocks inline instead of hiding them behind ctrl+o.",
        group=GROUP_THINKING,
        fn=_thinking_inline,
        anchors=('case"thinking":', "isTranscriptMode:", "latestThinkingSummary"),
    ),
]
