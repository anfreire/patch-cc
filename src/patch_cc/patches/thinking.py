"""Make thinking blocks visible in the normal UI instead of transcript-only."""

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


def _thinking_inline(content: str, _options: Options, outcome: Outcome) -> str:
    """Drop the transcript-only gate around thinking blocks.

    Two rewrites inside every ``case"thinking":`` arm that renders with an
    ``isTranscriptMode:`` prop: remove the early null-return, and force the
    renderer into transcript presentation (full markdown instead of the
    one-line collapsed form).
    """
    guard = outcome.step("null-guard")
    props = outcome.step("renderer-props")
    output = content
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
        anchors=('case"thinking":', "isTranscriptMode:"),
    ),
]
