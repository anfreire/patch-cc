"""Tool-call and diff rendering patches."""

from __future__ import annotations

import re

from .base import (
    GROUP_OUTPUT,
    IDENT,
    Options,
    Outcome,
    Patch,
    compile_js,
    splice,
    switch_case_end,
)

# --------------------------------------------------------------- tool calls

_COLLAPSED_RETURN = compile_js(
    rf'case"collapsed_read_search":return ({IDENT})\.createElement\(({IDENT}),\{{([^}}]*)\}}\)'
)
_COLLAPSED_CALL = compile_js(
    r"(?:createElement|jsx|jsxs)\("
    + IDENT
    + r",\{message:[^}]*inProgressToolUseIDs:[^}]*"
    r"shouldAnimate:[^}]*verbose:[^,}]+,tools:[^}]*lookups:[^}]*isActiveGroup:[^}]*\}\)"
)
_VERBOSE_PROP = compile_js(r"verbose:[^,}]+")


def _tool_call_verbose(content: str, _options: Options, outcome: Outcome) -> str:
    """Render collapsed read/search rows as if verbose mode were on."""

    def rewrite_return(match: re.Match[str]) -> str:
        ns, component, props = match.group(1), match.group(2), match.group(3)
        if "verbose:" not in props:
            return match.group(0)
        outcome.candidates += 1
        next_props = _VERBOSE_PROP.sub("verbose:!0", props, count=1)
        if next_props == props:
            return match.group(0)
        outcome.applied += 1
        return f'case"collapsed_read_search":return {ns}.createElement({component},{{{next_props}}})'

    output = _COLLAPSED_RETURN.sub(rewrite_return, content)

    # Newer builds use a block-form arm with a JSX-runtime call.
    needle = 'case"collapsed_read_search":{'
    index = 0
    while True:
        start = output.find(needle, index)
        if start == -1:
            break
        end = switch_case_end(output, start + len(needle))
        segment = output[start:end]
        index = start + len(needle)

        has_renderer = any(
            tok in segment for tok in ("createElement(", "jsx(", "jsxs(")
        )
        if not has_renderer or "verbose:" not in segment:
            continue
        if not _COLLAPSED_CALL.search(segment):
            continue

        outcome.candidates += 1
        next_segment = _VERBOSE_PROP.sub("verbose:!0", segment, count=1)
        if next_segment != segment:
            outcome.applied += 1
            output = splice(output, start, end, next_segment)
            index = start + len(next_segment)

    return output


# --------------------------------------------------------------- create diff

_CREATE_RETURN = compile_js(
    rf"return ({IDENT})\.(createElement|jsx|jsxs)\(({IDENT}),"
    rf"\{{filePath:({IDENT}),content:({IDENT}),verbose:({IDENT})\}}\)"
)
_UPDATE_RENDERER = compile_js(
    rf"(?:createElement|jsx|jsxs)\(({IDENT}),\{{filePath:[^}}]*structuredPatch:[^}}]*"
    rf"style:({IDENT}),verbose:{IDENT}"
)
_LINE_COUNTER = compile_js(
    rf"let {IDENT}=({IDENT})\({IDENT}\);return {IDENT}\.(?:createElement|jsxs)"
    rf"\({IDENT},(?:null,|\{{children:\[)\"Wrote \""
)
_ALREADY_CREATE_DIFF = "structuredPatch:[{oldStart:1,oldLines:0,newStart:1"


def _create_diff_colors(content: str, _options: Options, outcome: Outcome) -> str:
    """Render newly-created files through the diff component so lines get ``+``."""
    output = content
    index = 0
    create_needle, update_needle = 'case"create":', 'case"update":'

    while True:
        create_start = output.find(create_needle, index)
        if create_start == -1:
            break
        update_start = output.find(update_needle, create_start + len(create_needle))
        if update_start == -1:
            index = create_start + len(create_needle)
            continue

        switch_end = switch_case_end(output, update_start + len(update_needle))
        create_segment = output[create_start:update_start]
        update_segment = output[update_start:switch_end]
        index = update_start + len(update_needle)

        if _ALREADY_CREATE_DIFF in create_segment:
            continue

        create_match = _CREATE_RETURN.search(create_segment)
        if not create_match:
            continue
        update_match = _UPDATE_RENDERER.search(update_segment)
        if not update_match:
            continue

        outcome.candidates += 1
        ns, factory = create_match.group(1), create_match.group(2)
        file_var, content_var, verbose_var = create_match.group(4, 5, 6)
        diff_renderer, style_var = update_match.group(1), update_match.group(2)

        counter = _LINE_COUNTER.search(create_segment)
        line_count = (
            f"{counter.group(1)}({content_var})"
            if counter
            else f'{content_var}===""?0:{content_var}.split(`\\n`).length'
        )

        before = create_match.group(0)
        after = (
            f"return {ns}.{factory}({diff_renderer},{{"
            f"filePath:{file_var},structuredPatch:[{{oldStart:1,oldLines:0,newStart:1,"
            f"newLines:{line_count},"
            f'lines:{content_var}===""?[]:{content_var}.split(`\\n`)'
            f'.map((__cc_line)=>"+"+__cc_line)}}],'
            f"firstLine:{content_var}.split(`\\n`)[0]??null,"
            f'fileContent:"",style:{style_var},verbose:{verbose_var},previewHint:void 0}})'
        )

        next_segment = create_segment.replace(before, after, 1)
        if next_segment != create_segment:
            outcome.applied += 1
            output = splice(output, create_start, update_start, next_segment)
            index = create_start + len(next_segment)

    return output


PATCHES = [
    Patch(
        id="tool-calls",
        title="Detailed tool calls",
        summary="Show full read/search tool calls instead of collapsed one-line summaries.",
        group=GROUP_OUTPUT,
        fn=_tool_call_verbose,
        anchors=('case"collapsed_read_search"',),
    ),
    Patch(
        id="create-diff",
        title="Colour new files as diffs",
        summary="Render created files through the diff view so added lines keep + and green.",
        group=GROUP_OUTPUT,
        fn=_create_diff_colors,
        anchors=('case"create":', 'case"update":'),
    ),
]
