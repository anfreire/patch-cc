"""Live (streaming) thinking.

This is the single most fragile patch in the set: upstream has reshaped the
stream reducer at least three times, and most of their commit traffic lands
here. Two structural choices follow from that:

1. It is built from ~11 **named steps**, each recording its own outcome. A
   scalar hit count cannot tell "everything landed" from "half of it silently
   drifted", which is precisely how this patch hides its own regressions.
2. Steps share discovered identifiers through :class:`Discovery` rather than
   re-deriving them, and every step tolerates its own failure so one drifted
   shape does not take the rest down with it.

Tolerating failure is not the same as ignoring it: the steps the feature cannot
live without are marked *required*, and the three reducer shapes form a group of
which at least one must land. Everything else -- the legacy cleanups upstream has
since dropped -- stays optional, so `doctor` distinguishes "this build lacks that
shape" from "live thinking is dead".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .base import GROUP_LIVE, IDENT, Options, Outcome, Patch, compile_js, splice

#: The mutually-exclusive reducer variants. On any one build one of them lands;
#: none landing means upstream shipped a fourth shape and live thinking is dead.
REDUCER = "reducer"

#: The two rewrites that *are* live thinking: one creates the virtual message
#: when a thinking block opens, the other appends each delta to it. Recognising
#: a reducer proves nothing on its own -- a variant whose incidental rewrites
#: (threading the setter, clearing state on message_stop) landed while these two
#: drifted reports plenty of hits and streams nothing. Each is checked by the
#: marker its own builder emits, so the test is "did the state update reach the
#: bundle", not "did some literal match".
_CORE_UPDATES = (
    ("block-start", "__cc_streamingThinkingMessage="),
    ("thinking-delta", "__cc_nextStreamingThinkingDelta"),
)


def _record_core_updates(segment: str, outcome: Outcome) -> None:
    """Credit the core state updates found in a rewritten reducer body."""
    for name, marker in _CORE_UPDATES:
        if marker in segment:
            step = outcome.step(name, expect=True)
            step.candidates += 1
            step.applied += 1


@dataclass(slots=True)
class Discovery:
    """Identifiers found in one step and consumed by later ones."""

    streaming_var: str | None = None
    create_message_helper: str | None = None
    transcript_var: str | None = None


# --------------------------------------------------------------- JS builders


def _reset(setter: str, ended_at: str) -> str:
    """The `mark the live block finished` state update, used by many cases."""
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>__cc_prevStreamingThinking?"
        f"{{...__cc_prevStreamingThinking,isStreaming:!1,streamingEndedAt:{ended_at},"
        f"currentIndex:null,currentMessage:null}}:__cc_prevStreamingThinking)"
    )


def _block_start(event: str, setter: str, helper: str) -> str:
    """Create a virtual message when a thinking content block starts.

    Keyed by content-block index, so a block-start handled twice replaces its
    entry rather than appending a duplicate live block.
    """
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>{{"
        f"let __cc_streamingThinkingMessage={helper}({{content:["
        f'{event}.event.content_block.type==="redacted_thinking"'
        f'?{{type:"redacted_thinking",data:{event}.event.content_block.data??""}}'
        f':{{type:"thinking",thinking:""}}],isVirtual:!0}}),'
        f"__cc_nextStreamingThinkingMessages=[...(__cc_prevStreamingThinking?.messages??[])"
        f".filter((__cc_entry)=>__cc_entry.index!=={event}.event.index),"
        f"{{index:{event}.event.index,message:__cc_streamingThinkingMessage}}];"
        f'return{{thinking:{event}.event.content_block.type==="redacted_thinking"'
        f'?{event}.event.content_block.data??"":"",isStreaming:!0,streamingEndedAt:void 0,'
        f"currentIndex:{event}.event.index,currentMessage:__cc_streamingThinkingMessage,"
        f"messages:__cc_nextStreamingThinkingMessages}}}})"
    )


def _delta(event: str, setter: str, helper: str) -> str:
    """Append a thinking delta to the live block."""
    return (
        f"{setter}?.((__cc_prevStreamingThinking)=>{{"
        f"let __cc_nextStreamingThinkingDelta=typeof {event}.event.delta.thinking==="
        f'"string"?{event}.event.delta.thinking:"",'
        f'__cc_nextStreamingThinkingText=(__cc_prevStreamingThinking?.thinking??"")'
        f"+__cc_nextStreamingThinkingDelta,"
        f"__cc_nextStreamingThinkingIndex=__cc_prevStreamingThinking?.currentIndex"
        f"??{event}.event.index,"
        f"__cc_nextStreamingThinkingMessage={helper}({{content:["
        f'{{type:"thinking",thinking:__cc_nextStreamingThinkingText}}],isVirtual:!0}}),'
        f"__cc_nextStreamingThinkingMessages=[...(__cc_prevStreamingThinking?.messages??[])"
        f".filter((__cc_entry)=>__cc_entry.index!==__cc_nextStreamingThinkingIndex),"
        f"{{index:__cc_nextStreamingThinkingIndex,message:__cc_nextStreamingThinkingMessage}}];"
        f"return __cc_prevStreamingThinking?{{...__cc_prevStreamingThinking,"
        f"thinking:__cc_nextStreamingThinkingText,isStreaming:!0,streamingEndedAt:void 0,"
        f"currentIndex:__cc_nextStreamingThinkingIndex,"
        f"currentMessage:__cc_nextStreamingThinkingMessage,"
        f"messages:__cc_nextStreamingThinkingMessages}}"
        f":{{thinking:__cc_nextStreamingThinkingText,isStreaming:!0,streamingEndedAt:void 0,"
        f"currentIndex:{event}.event.index,"
        f"currentMessage:__cc_nextStreamingThinkingMessage,"
        f"messages:[{{index:{event}.event.index,"
        f"message:__cc_nextStreamingThinkingMessage}}]}}}})"
    )


# ------------------------------------------------------------------- step 1

_MEMO_CACHE = compile_js(
    rf"if\(({IDENT})\[(\d+)\]!==({IDENT})\|\|\1\[(\d+)\]!==({IDENT})\|\|\1\[(\d+)\]!==({IDENT})\)"
    rf"([\s\S]{{0,700}}?thinking:\5\.thinking[\s\S]{{0,700}}?)"
    rf"\1\[\2\]=\3,\1\[\4\]=\5,\1\[\6\]=\7,(\1\[\d+\]={IDENT};)"
)


def _step_memo_cache(content: str, outcome: Outcome) -> str:
    """Key the memo cache on `thinking?.thinking`, not the wrapper object.

    Without this the comparator sees the same object identity across deltas and
    never re-renders while text is still streaming in.
    """
    step = outcome.step("memo-cache")

    def rewrite(match: re.Match[str]) -> str:
        cache, i1, v1, i2, v2, i3, v3, middle, tail = match.groups()
        step.candidates += 1
        if f"{v2}?.thinking" in match.group(0):
            return match.group(0)
        step.applied += 1
        return (
            f"if({cache}[{i1}]!=={v1}||{cache}[{i2}]!=={v2}?.thinking||{cache}[{i3}]!=={v3})"
            f"{middle}{cache}[{i1}]={v1},{cache}[{i2}]={v2}?.thinking,{cache}[{i3}]={v3},{tail}"
        )

    return _MEMO_CACHE.sub(rewrite, content)


# ------------------------------------------------------------------- step 2

#: How far back the state back-scan looks for the setter's ``useState(null)``.
#: Deliberately *not* widened past the worst observed reach (35k on 2.1.217) by
#: much: minified setter names repeat across scopes, so a wider window trades a
#: loud discovery failure -- `prop-threading` is required, so it stops the patch
#: -- for the chance of silently binding an unrelated same-named state. The step
#: notes the distance actually reached, which is the early warning instead.
_DISCOVER_WINDOW = 50_000

_HIDE_PAST = compile_js(rf"hidePastThinking:!0,streamingThinking:({IDENT})")
_ON_STREAMING = compile_js(rf"onStreamingThinking:({IDENT})")
_CREATE_ELEMENT_CALL = compile_js(rf"createElement\(({IDENT}),\{{([^{{}}]*?)\}}\)")
_PROMPT_RENDERER = compile_js(
    rf"createElement\(({IDENT}),\{{([\s\S]{{0,2000}}?placeholderElement:[\s\S]{{0,2000}}?"
    rf"agentDefinitions:[^}}]*?onOpenRateLimitOptions:[^}}]*?isLoading:)([^,}}]+)"
    rf"(,streamingText:[^}}]*?(?:showThinkingHint:[^}}]*?)?isBriefOnly:[^}}]*?)\}}\)"
)
_JSX_MAIN_PROPS = compile_js(
    r"(screen:[^,}]+,streamingToolUses:[^,}]+,)"
    r"(showAllInTranscript:[^,}]+,agentDefinitions:[^,}]+,onOpenRateLimitOptions:[^,}]+,isLoading:[^,}]+)"
)
_JSX_TRANSCRIPT_PROPS = compile_js(
    r"(screen:[^,}]+,agentDefinitions:[^,}]+,streamingToolUses:[^,}]+,)"
    r"(showAllInTranscript:[^,}]+,onOpenRateLimitOptions:[^,}]+,isLoading:[^,}]+)"
)


def _discover_streaming_var(content: str, found: Discovery, outcome: Outcome) -> None:
    """Find the state variable holding live thinking.

    2.1.216 no longer ships `hidePastThinking`, so the primary anchor is already
    dead and we rely on the `onStreamingThinking` -> `useState(null)` back-scan.
    Losing that fallback too would leave this patch with nothing, so the scan
    records how far back it had to reach: that distance against
    :data:`_DISCOVER_WINDOW` is the warning that the next hoist will break it.
    """
    primary = _HIDE_PAST.search(content)
    if primary:
        found.streaming_var = primary.group(1)
        return

    step = outcome.step("discover")
    step.note("hidePastThinking anchor gone; using useState back-scan")
    for match in _ON_STREAMING.finditer(content):
        setter = match.group(1)
        start = max(0, match.start() - _DISCOVER_WINDOW)
        window = content[start : match.start()]
        state = compile_js(
            rf"\[({IDENT}),{re.escape(setter)}\]={IDENT}\.useState\(null\)"
        )
        candidates = list(state.finditer(window))
        if candidates:
            found.streaming_var = candidates[-1].group(1)
            reach = len(window) - candidates[-1].start()
            step.note(f"resolved {reach:,} chars back of {_DISCOVER_WINDOW:,}")
            if len(candidates) > 1:
                # Nearest wins, which is a guess the moment there is more than
                # one. Both shipped builds have exactly one state destructured
                # to this setter in the *whole* bundle; a second would mean the
                # pairing by setter name has stopped being an identification.
                step.note(
                    f"{len(candidates)} states share this setter; took the nearest"
                )
            return


def _step_prop_threading(content: str, found: Discovery, outcome: Outcome) -> str:
    """Pass the live-thinking state into the renderers that need it."""
    step = outcome.step("prop-threading", expect=True)
    if found.streaming_var is None:
        step.note("no streaming state variable found; skipped")
        return content
    var = found.streaming_var

    def rewrite_create_element(match: re.Match[str]) -> str:
        component, props = match.group(1), match.group(2)
        required = (
            "streamingToolUses:",
            "toolJSX:",
            "agentDefinitions:",
            "onOpenRateLimitOptions:",
            "conversationId:",
            "isLoading:",
        )
        forbidden = ("streamingThinking:", "hidePastThinking:")
        if any(tok not in props for tok in required) or any(
            t in props for t in forbidden
        ):
            return match.group(0)
        step.candidates += 1
        step.applied += 1
        return f"createElement({component},{{{props},streamingThinking:{var}}})"

    output = _CREATE_ELEMENT_CALL.sub(rewrite_create_element, content)

    def rewrite_prompt(match: re.Match[str]) -> str:
        if "streamingThinking:" in match.group(0):
            return match.group(0)
        component, before, is_loading, after = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"createElement({component},{{{before}{is_loading},"
            f"streamingThinking:{var}{after}}})"
        )

    output = _PROMPT_RENDERER.sub(rewrite_prompt, output)

    def inject(match: re.Match[str]) -> str:
        if "streamingThinking:" in match.group(0):
            return match.group(0)
        step.candidates += 1
        step.applied += 1
        return f"{match.group(1)}streamingThinking:{var},{match.group(2)}"

    output = _JSX_MAIN_PROPS.sub(inject, output)
    return _JSX_TRANSCRIPT_PROPS.sub(inject, output)


# ------------------------------------------------------------------- step 3

_THINKING_DISPLAY = compile_js(
    rf"({IDENT})=({IDENT})\.type!==\"disabled\"&&!({IDENT})"
    rf"\(process\.env\.CLAUDE_CODE_DISABLE_THINKING\),({IDENT})=\1"
    rf"(?:&&{IDENT}\(\)&&{IDENT}\({IDENT}\))?\?\2\.display(?:\?\?void 0)?:void 0,({IDENT})=void 0;"
)

# 2.1.216 hoists the env check into its own variable and gates the display
# value behind extra feature/model helpers. The helper chain is kept verbatim;
# only the display expression gains the `??"summarized"` default.
_THINKING_DISPLAY_2 = compile_js(
    rf"({IDENT})=({IDENT})\(process\.env\.CLAUDE_CODE_DISABLE_THINKING\),"
    rf"({IDENT})=({IDENT})\.type!==\"disabled\"&&!\1,"
    rf"({IDENT})=\3((?:&&{IDENT}\((?:{IDENT})?\))*)\?\4\.display:void 0,"
)


def _step_display_mode(content: str, outcome: Outcome) -> str:
    """Default the thinking request to `summarized`.

    Without a display mode in the request the API streams signature-only (or
    late) thinking, so the live row starves -- worst on short thinks. Upstream
    only requests summaries when the `showThinkingSummaries` setting is on;
    default it on instead.
    """
    step = outcome.step("display-mode", expect=True)

    def rewrite(match: re.Match[str]) -> str:
        enabled, config, env_helper, display, request = match.groups()
        step.candidates += 1
        if 'display??"summarized"' in match.group(0):
            return match.group(0)
        step.applied += 1
        return (
            f'{enabled}={config}.type!=="disabled"&&!{env_helper}'
            f"(process.env.CLAUDE_CODE_DISABLE_THINKING),"
            f'{display}={enabled}?{config}.display??"summarized":void 0,{request}=void 0;'
        )

    output = _THINKING_DISPLAY.sub(rewrite, content)

    def rewrite_hoisted(match: re.Match[str]) -> str:
        env_var, env_helper, enabled, config, display, guards = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"{env_var}={env_helper}(process.env.CLAUDE_CODE_DISABLE_THINKING),"
            f'{enabled}={config}.type!=="disabled"&&!{env_var},'
            f'{display}={enabled}{guards}?{config}.display??"summarized":void 0,'
        )

    return _THINKING_DISPLAY_2.sub(rewrite_hoisted, output, count=1)


# ------------------------------------------------------------------- step 4

# The braces around the guarded call are optional *as a pair*: a bare `\}?` tail
# would happily eat the enclosing block's closing brace on the unbraced shape and
# the rewrite -- which emits its own -- would not put it back. Hence the
# conditional: consume the closing brace only if the opening one was there.
_ASSISTANT_THINKING = compile_js(
    rf"let ({IDENT})=({IDENT})\.message\.content\.find\(\(({IDENT})\)=>"
    rf'\3\.type==="thinking"\);if\(\1&&\1\.type==="thinking"\)(\{{)?({IDENT})'
    rf"\?\.\(\(\)=>\(\{{thinking:\1\.thinking,isStreaming:!1,"
    rf"streamingEndedAt:Date\.now\(\)\}}\)\)(?(4)\}})"
)


def _step_final_summary(content: str, outcome: Outcome) -> str:
    """Include redacted thinking in the final assistant-message summary."""
    step = outcome.step("final-summary")

    def rewrite(match: re.Match[str]) -> str:
        block, message, item, _brace, setter = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"let {block}={message}.message.content.find(({item})=>"
            f'{item}.type==="thinking"||{item}.type==="redacted_thinking");'
            f'if({block}&&({block}.type==="thinking"||{block}.type==="redacted_thinking"))'
            f'{setter}?.(()=>({{thinking:{block}.type==="thinking"'
            f'?{block}.thinking:{block}.data??"",isStreaming:!1,'
            f"streamingEndedAt:Date.now()}}))"
        )

    return _ASSISTANT_THINKING.sub(rewrite, content)


# ------------------------------------------------------------------- step 5

_MEMO_ASSIGN = compile_js(rf"({IDENT})=({IDENT})\.memo\(({IDENT}),({IDENT})\)")


def _step_memo_removal(content: str, outcome: Outcome) -> str:
    """Unwrap the message-row memo whose comparator suppresses live updates."""
    step = outcome.step("memo-removal")
    output = content
    pos = 0
    while True:
        match = _MEMO_ASSIGN.search(output, pos)
        if not match:
            break
        lhs, _ns, render_fn, comparator = match.groups()
        pos = match.end()

        start = output.find(f"function {comparator}(")
        if start == -1:
            continue
        body = output[start : start + 2200]
        if not all(
            tok in body
            for tok in (
                ".screen!==",
                ".columns!==",
                ".lastThinkingBlockId",
                ".streamingToolUseIDs",
            )
        ):
            continue

        step.candidates += 1
        replacement = f"{lhs}={render_fn}"
        if replacement != match.group(0):
            output = splice(output, match.start(), match.end(), replacement)
            step.applied += 1
            pos = match.start() + len(replacement)
    return output


# ------------------------------------------------------------------- step 6

_LINGER_LABEL = compile_js(
    rf"({IDENT}):\{{if\(!({IDENT})\)\{{({IDENT})=!1;break \1\}}"
    rf"if\(\2\.isStreaming\)\{{\3=!0;break \1\}}"
    rf"if\(\2\.streamingEndedAt\)\{{\3=Date\.now\(\)-\2\.streamingEndedAt<30000;break \1\}}"
    rf"\3=!1\}}let ({IDENT})=\3"
)
_LINGER_MEMO = compile_js(
    rf"({IDENT})=({IDENT})\.useMemo\(\(\)=>\{{if\(!({IDENT})\)return!1;"
    rf"if\(\3\.isStreaming\)return!0;"
    rf"if\(\3\.streamingEndedAt\)return Date\.now\(\)-\3\.streamingEndedAt<30000;"
    rf"return!1\}},\[\3\]\)"
)


def _step_linger(content: str, outcome: Outcome) -> str:
    """Drop the 30-second post-stream linger; show only while streaming."""
    step = outcome.step("linger")

    def rewrite_label(match: re.Match[str]) -> str:
        step.candidates += 1
        step.applied += 1
        return (
            f"let {match.group(4)}=!!({match.group(2)}&&{match.group(2)}.isStreaming)"
        )

    def rewrite_memo(match: re.Match[str]) -> str:
        visible, ns, stream = match.groups()
        step.candidates += 1
        step.applied += 1
        return (
            f"{visible}={ns}.useMemo(()=>!!({stream}&&{stream}.isStreaming),[{stream}])"
        )

    output = _LINGER_LABEL.sub(rewrite_label, content)
    return _LINGER_MEMO.sub(rewrite_memo, output)


# ------------------------------------------------------------------- step 7

_TOOLUSE_HELPERS = compile_js(
    rf"let {IDENT}=({IDENT})\(\{{content:\[{IDENT}\.contentBlock\]\}}\);"
    rf"return {IDENT}\.uuid=({IDENT})\({IDENT}\.contentBlock\.id,0\),({IDENT})\(\[{IDENT}\]\)"
)
_RENDERER_HAS_VAR = compile_js(
    rf"\(\{{messages:[^}}]*?streamingToolUses:{IDENT},streamingThinking:({IDENT}),showAllInTranscript:"
)
_RENDERER_SIGNATURE = compile_js(
    rf"(\(\{{messages:[^}}]*?streamingToolUses:{IDENT},)(showAllInTranscript:)"
)
_TRANSCRIPT_VAR = compile_js(
    rf"streamingToolUses:{IDENT},[^}}]*streamingThinking:({IDENT}),streamingText:"
)


def _step_transcript_signature(content: str, found: Discovery, outcome: Outcome) -> str:
    """Make sure the transcript renderer actually receives the live state."""
    step = outcome.step("transcript-signature", expect=True)

    helpers = _TOOLUSE_HELPERS.search(content)
    if helpers:
        found.create_message_helper = helpers.group(1)

    existing = _RENDERER_HAS_VAR.search(content)
    if existing:
        found.transcript_var = existing.group(1)
        step.candidates += 1
        step.applied += 1  # nothing to do; upstream already threads it
        return content

    output = content
    if found.streaming_var is not None:

        def inject(match: re.Match[str]) -> str:
            if "streamingThinking:" in match.group(0):
                return match.group(0)
            step.candidates += 1
            step.applied += 1
            found.transcript_var = "__cc_streamingThinking"
            return f"{match.group(1)}streamingThinking:__cc_streamingThinking,{match.group(2)}"

        output = _RENDERER_SIGNATURE.sub(inject, output, count=1)

    if found.transcript_var is None:
        fallback = _TRANSCRIPT_VAR.search(output)
        if fallback:
            found.transcript_var = fallback.group(1)
    return output


# ------------------------------------------------------------------- step 8

_INLINE_EXTRAS = compile_js(
    rf"({IDENT})=({IDENT})\.useMemo\(\(\)=>({IDENT})\.flatMap\(\(({IDENT})\)=>\{{"
    rf"let ({IDENT})=({IDENT})\(\{{content:\[\4\.contentBlock\]\}}\);"
    rf"return \5\.uuid=({IDENT})\(\4\.contentBlock\.id,0\),({IDENT})\(\[\5\]\)\}}\),\[\3\]\)"
)


def _step_inline_extras(content: str, found: Discovery, outcome: Outcome) -> str:
    """Render live thinking inline, ordered with streaming tool-use blocks."""
    step = outcome.step("inline-extras", expect=True)
    if not found.transcript_var:
        step.note("no transcript streaming variable; skipped")
        return content
    var = found.transcript_var

    def rewrite(match: re.Match[str]) -> str:
        extras, ns, tool_uses, entry, message, helper, uuid_helper, normalize = (
            match.groups()
        )
        step.candidates += 1
        step.applied += 1
        found.create_message_helper = helper
        return (
            f"{extras}={ns}.useMemo(()=>{{"
            f"let __cc_streamingToolUseExtras={tool_uses}.map(({entry})=>{{"
            f"let {message}={helper}({{content:[{entry}.contentBlock]}});"
            f"return {message}.uuid={uuid_helper}({entry}.contentBlock.id,0),"
            f"{{index:{entry}.index??9007199254740991,"
            f"messages:{normalize}([{message}])}}}}),"
            f"__cc_streamingThinkingExtras=({var}?.messages??[])"
            f".map((__cc_entry,__cc_index)=>({{"
            f"index:__cc_entry.index??9007199254740991+__cc_index,"
            f"messages:{normalize}([__cc_entry.message??__cc_entry])}}));"
            f"return[...__cc_streamingToolUseExtras,...__cc_streamingThinkingExtras]"
            f".sort((__cc_a,__cc_b)=>__cc_a.index===__cc_b.index?0:__cc_a.index-__cc_b.index)"
            f".flatMap((__cc_entry)=>__cc_entry.messages)}},[{tool_uses},{var}])"
        )

    return _INLINE_EXTRAS.sub(rewrite, content)


# ------------------------------------------------------------------- step 9

_LIVE_ROW = compile_js(
    rf"({IDENT})&{{2}}({IDENT})&{{2}}!({IDENT})&{{2}}({IDENT})\.createElement\(({IDENT}),"
    rf"\{{marginTop:1\}},\4\.createElement\(({IDENT}),\{{param:\{{type:\"thinking\","
    rf"thinking:\2\.thinking\}},addMargin:!1,isTranscriptMode:!0,verbose:({IDENT}),"
    rf"hideInTranscript:!1\}}\)\)"
)


def _step_bottom_row(content: str, outcome: Outcome) -> str:
    """Remove the separate bottom-pinned live row now that it renders inline."""
    step = outcome.step("bottom-row")

    def rewrite(_match: re.Match[str]) -> str:
        step.candidates += 1
        step.applied += 1
        return "null"

    return _LIVE_ROW.sub(rewrite, content)


# ------------------------------------------------------ steps 10-11: reducer

_PROGRESS_ONLY = (
    r'case"thinking_delta":\{{let\{{delta:({ident})\}}={event}\.event;'
    r'if\("estimated_tokens"in \1&&typeof \1\.estimated_tokens==="number"\)'
    r'({ident})\?\.\(\{{type:"thinking_progress",'
    r"estimatedTokensDelta:\1\.estimated_tokens\}}\);return\}}"
)
_PROGRESS_WITH_TEXT = (
    r'case"thinking_delta":\{{let\{{delta:({ident})\}}={event}\.event;'
    r'if\("estimated_tokens"in \1&&typeof \1\.estimated_tokens==="number"\)'
    r'({ident})\?\.\(\{{type:"thinking_progress",'
    r"estimatedTokensDelta:\1\.estimated_tokens\}}\);"
    r'else if\("thinking"in \1&&typeof \1\.thinking==="string"&&\1\.thinking\.length>0\)'
    r'\2\?\.\(\{{type:"thinking_progress",'
    r"estimatedTokensDelta:({ident})\(\1\.thinking\)\}}\);return\}}"
)


def _apply_pairs(segment: str, pairs: list[tuple[str, str]], step: Outcome) -> str:
    """Apply literal before/after rewrites, counting each independently."""
    for before, after in pairs:
        if before and before in segment:
            step.candidates += 1
            segment = segment.replace(before, after, 1)
            if after in segment:
                step.applied += 1
    return segment


def _apply_progress_variants(
    segment: str, event: str, delta_body: str, step: Outcome
) -> str:
    """Keep upstream's thinking-progress metrics while adding our state update."""
    for template in (_PROGRESS_ONLY, _PROGRESS_WITH_TEXT):
        pattern = compile_js(template.format(ident=IDENT, event=re.escape(event)))

        def rewrite(match: re.Match[str]) -> str:
            groups = match.groups()
            delta_var, metrics = groups[0], groups[1]
            tail = (
                f"let{{delta:{delta_var}}}={event}.event;"
                f'if("estimated_tokens"in {delta_var}&&'
                f'typeof {delta_var}.estimated_tokens==="number")'
                f'{metrics}?.({{type:"thinking_progress",'
                f"estimatedTokensDelta:{delta_var}.estimated_tokens}});"
            )
            if len(groups) > 2:
                estimator = groups[2]
                tail += (
                    f'else if("thinking"in {delta_var}&&'
                    f'typeof {delta_var}.thinking==="string"&&'
                    f"{delta_var}.thinking.length>0)"
                    f'{metrics}?.({{type:"thinking_progress",'
                    f"estimatedTokensDelta:{estimator}({delta_var}.thinking)}});"
                )
            return f'case"thinking_delta":{{{delta_body}{tail}return}}'

        updated = pattern.sub(rewrite, segment, count=1)
        if updated != segment:
            step.candidates += 1
            step.applied += 1
            segment = updated
    return segment


def _reducer_pairs(
    event: str,
    setter: str,
    mode: str,
    tools: str,
    helper: str,
    *,
    optional: bool,
    options_param: str | None = None,
    display_transform: str | None = None,
) -> list[tuple[str, str]]:
    """Before/after rewrites for one stream-reducer shape.

    ``optional`` selects whether upstream calls the setters directly or through
    ``?.`` -- both spellings exist in the wild.
    """
    call = "?." if optional else ""
    ended = _reset(setter, "Date.now()")
    cleared = _reset(setter, "void 0")
    pairs: list[tuple[str, str]] = [
        (
            f'if({event}.type==="stream_request_start"){{{mode}("requesting");return}}',
            f'if({event}.type==="stream_request_start"){{{setter}?.(null),{mode}{call}("requesting");return}}',
        ),
        (
            f'if({event}.type==="stream_request_start"){{{mode}?.("requesting");return}}',
            f'if({event}.type==="stream_request_start"){{{setter}?.(null),{mode}?.("requesting");return}}',
        ),
    ]

    if options_param:
        pairs.append(
            (
                f'if({event}.event.type==="message_stop"){{{options_param}.displayTransform?.finalize(),'
                f'{mode}("tool-use"),{tools}(()=>[]);return}}',
                f'if({event}.event.type==="message_stop"){{{options_param}.displayTransform?.finalize(),'
                f'{ended},{mode}("tool-use"),{tools}(()=>[]);return}}',
            )
        )
    if display_transform:
        for prefix in (
            f"{display_transform}.finalize()",
            f"{display_transform}?.finalize()",
        ):
            for mo, to in ((mode, tools), (f"{mode}?.", f"{tools}?.")):
                pairs.append(
                    (
                        f'if({event}.event.type==="message_stop"){{{prefix},'
                        f'{mo}("tool-use"),{to}(()=>[]);return}}',
                        f'if({event}.event.type==="message_stop"){{{display_transform}?.finalize(),'
                        f'{ended},{mode}?.("tool-use"),{tools}?.(()=>[]);return}}',
                    )
                )

    pairs += [
        (
            f'if({event}.event.type==="message_stop"){{{mode}("tool-use"),{tools}(()=>[]);return}}',
            f'if({event}.event.type==="message_stop"){{{ended},{mode}{call}("tool-use"),'
            f"{tools}{call}(()=>[]);return}}",
        ),
        (
            f'if({event}.event.type==="message_stop"){{{mode}?.("tool-use"),{tools}?.(()=>[]);return}}',
            f'if({event}.event.type==="message_stop"){{{ended},{mode}?.("tool-use"),'
            f"{tools}?.(()=>[]);return}}",
        ),
        (
            f'case"thinking":case"redacted_thinking":{mode}("thinking");return;',
            f'case"thinking":case"redacted_thinking":{_block_start(event, setter, helper)},'
            f'{mode}{call}("thinking");return;',
        ),
        (
            f'case"thinking":case"redacted_thinking":{mode}?.("thinking");return;',
            f'case"thinking":case"redacted_thinking":{_block_start(event, setter, helper)},'
            f'{mode}?.("thinking");return;',
        ),
        (
            f'case"text":{mode}("responding");return;',
            f'case"text":{cleared},{mode}{call}("responding");return;',
        ),
        (
            f'case"text":{mode}?.("responding");return;',
            f'case"text":{cleared},{mode}?.("responding");return;',
        ),
        (
            f'case"message_delta":if({mode}("responding"),{event}.event.usage.output_tokens!=null)',
            f'case"message_delta":if({cleared},{mode}("responding"),'
            f"{event}.event.usage.output_tokens!=null)",
        ),
        (
            f'case"message_delta":{mode}("responding");return;',
            f'case"message_delta":{cleared},{mode}{call}("responding");return;',
        ),
        (
            f'case"message_delta":{mode}?.("responding");return;',
            f'case"message_delta":{cleared},{mode}?.("responding");return;',
        ),
        (
            f'case"message_delta":{{{mode}("responding");',
            f'case"message_delta":{{{cleared},{mode}{call}("responding");',
        ),
        (
            f'case"message_delta":{{{mode}?.("responding");',
            f'case"message_delta":{{{cleared},{mode}?.("responding");',
        ),
    ]
    return pairs


_DESTRUCTURED_HANDLER = compile_js(
    rf"function {IDENT}\(({IDENT}),({IDENT})\)\{{let\{{([^}}]*onStreamingThinking:{IDENT}[^}}]*)\}}=\2;"
)
_MISSING_HANDLER = compile_js(
    rf"function {IDENT}\(({IDENT}),({IDENT})(?:,{IDENT})?\)\{{let\{{([^}}]*)\}}=\2;"
)
_LEGACY_ANCHOR = 'type!=="stream_event"&&'
_FN_SIG = compile_js(rf"^function {IDENT}\(([^)]*)\)\{{")


def _prop_var(props: str, name: str, *, shorthand: bool = False) -> str | None:
    alias = compile_js(rf"{re.escape(name)}:({IDENT})").search(props)
    if alias:
        return alias.group(1)
    if shorthand and compile_js(rf"(?:^|,){re.escape(name)}(?:,|$)").search(props):
        return name
    return None


def _handler_is_stream_reducer(segment: str) -> bool:
    return all(
        tok in segment
        for tok in (
            'type==="stream_request_start"',
            'case"thinking_delta"',
            "content_block_start",
        )
    )


def _step_reducer_destructured(content: str, found: Discovery, outcome: Outcome) -> str:
    """2.1.138+ shape: options bag that already destructures onStreamingThinking."""
    step = outcome.step("reducer-destructured", expect=REDUCER)
    helper = found.create_message_helper
    if helper is None:
        step.note("no virtual-message helper discovered; skipped")
        return content

    output, pos = content, 0
    while True:
        match = _DESTRUCTURED_HANDLER.search(output, pos)
        if not match:
            break
        event, options_param, props = match.groups()
        pos = match.end()

        mode = _prop_var(props, "onSetStreamMode")
        tools = _prop_var(props, "onStreamingToolUses")
        setter = _prop_var(props, "onStreamingThinking")
        if not (mode and tools and setter):
            continue

        end = output.find("function ", match.end())
        if end == -1:
            continue
        segment = output[match.start() : end]
        if not _handler_is_stream_reducer(segment):
            continue

        delta_body = _delta(event, setter, helper) + ";"
        pairs = _reducer_pairs(
            event,
            setter,
            mode,
            tools,
            helper,
            optional=False,
            options_param=options_param,
        )
        pairs.append(
            (
                'case"thinking_delta":return;',
                f'case"thinking_delta":{{{delta_body}return;}}',
            )
        )

        updated = _apply_pairs(segment, pairs, step)
        updated = _apply_progress_variants(updated, event, delta_body, step)
        if updated != segment:
            _record_core_updates(updated, outcome)
            output = splice(output, match.start(), end, updated)
            pos = match.start() + len(updated)
    return output


def _step_reducer_inner(content: str, found: Discovery, outcome: Outcome) -> str:
    """2.1.183+ shape: inner handler that dropped onStreamingThinking."""
    step = outcome.step("reducer-inner", expect=REDUCER)
    helper = found.create_message_helper
    if helper is None:
        step.note("no virtual-message helper discovered; skipped")
        return content

    setter = "__cc_onStreamingThinking"
    output, pos = content, 0
    while True:
        match = _MISSING_HANDLER.search(output, pos)
        if not match:
            break
        event, options_param, props = match.groups()
        pos = match.end()
        if "onStreamingThinking:" in props:
            continue

        mode = _prop_var(props, "onSetStreamMode", shorthand=True)
        tools = _prop_var(props, "onStreamingToolUses", shorthand=True)
        display = _prop_var(props, "displayTransform", shorthand=True)
        if not (mode and tools):
            continue

        end = output.find("function ", match.end())
        if end == -1:
            continue
        segment = output[match.start() : end]
        if not _handler_is_stream_reducer(segment):
            continue

        delta_body = _delta(event, setter, helper) + ";"
        pairs = [
            (
                f"let{{{props}}}={options_param};",
                f"let{{{props},onStreamingThinking:{setter}}}={options_param};",
            )
        ]
        pairs += _reducer_pairs(
            event, setter, mode, tools, helper, optional=True, display_transform=display
        )
        pairs.append(
            (
                'case"thinking_delta":return;',
                f'case"thinking_delta":{{{delta_body}return;}}',
            )
        )

        updated = _apply_pairs(segment, pairs, step)
        updated = _apply_progress_variants(updated, event, delta_body, step)
        if updated != segment:
            _record_core_updates(updated, outcome)
            output = splice(output, match.start(), end, updated)
            pos = match.start() + len(updated)
    return output


def _step_reducer_legacy(content: str, found: Discovery, outcome: Outcome) -> str:
    """Pre-2.1.138 shape: positional parameters, no options bag."""
    step = outcome.step("reducer-legacy", expect=REDUCER)
    anchor = content.find(_LEGACY_ANCHOR)
    if anchor == -1:
        return content
    if content.find('type==="stream_request_start"', anchor) == -1:
        return content
    if content.find('case"thinking_delta"', anchor) == -1:
        return content

    start = content.rfind("function ", 0, anchor)
    end = content.find("function ", anchor + len(_LEGACY_ANCHOR))
    if start == -1 or end == -1:
        return content

    segment = content[start:end]
    signature = _FN_SIG.search(segment)
    if not signature:
        return content
    params = [p.strip() for p in signature.group(1).split(",")]
    if len(params) < 7:
        return content

    event, append_output, mode, tools, setter = (
        params[0],
        params[2],
        params[3],
        params[4],
        params[6],
    )
    helper = found.create_message_helper

    pairs = _reducer_pairs(event, setter, mode, tools, helper or "", optional=False)
    if helper is None:
        # Without the helper we cannot synthesise virtual messages; keep only
        # the rewrites that do not need it.
        pairs = [p for p in pairs if "__cc_streamingThinkingMessage" not in p[1]]
    else:
        delta_body = _delta(event, setter, helper) + ";"
        pairs += [
            (
                f'case"thinking_delta":{append_output}({event}.event.delta.thinking);return;',
                f'case"thinking_delta":{{{append_output}({event}.event.delta.thinking);'
                f"{delta_body}return;}}",
            ),
            (
                'case"thinking_delta":return;',
                f'case"thinking_delta":{{{delta_body}return;}}',
            ),
        ]

    updated = _apply_pairs(segment, pairs, step)
    if helper is not None:
        updated = _apply_progress_variants(
            updated, event, _delta(event, setter, helper) + ";", step
        )
    if updated == segment:
        return content
    _record_core_updates(updated, outcome)
    return splice(content, start, end, updated)


# ------------------------------------------------------------------ assembly


def _live_thinking(content: str, _options: Options, outcome: Outcome) -> str:
    found = Discovery()
    # Declared before anything runs: an expectation that only comes into
    # existence once its own rewrite succeeds can never report that rewrite
    # missing -- which is exactly the silence being designed out here.
    for name, _marker in _CORE_UPDATES:
        outcome.step(name, expect=True)
    output = _step_memo_cache(content, outcome)
    _discover_streaming_var(output, found, outcome)
    output = _step_prop_threading(output, found, outcome)
    output = _step_display_mode(output, outcome)
    output = _step_final_summary(output, outcome)
    output = _step_memo_removal(output, outcome)
    output = _step_linger(output, outcome)
    output = _step_transcript_signature(output, found, outcome)
    output = _step_inline_extras(output, found, outcome)
    output = _step_bottom_row(output, outcome)
    output = _step_reducer_destructured(output, found, outcome)
    output = _step_reducer_inner(output, found, outcome)
    output = _step_reducer_legacy(output, found, outcome)

    if found.streaming_var is None:
        outcome.note("live-thinking state variable was never found")
    return output


PATCHES = [
    Patch(
        id="live-thinking",
        title="Stream thinking live",
        summary="Show thinking as it is generated, inline and in order, instead of "
        "only after the turn finishes.",
        group=GROUP_LIVE,
        fn=_live_thinking,
        anchors=(
            "onStreamingThinking:",
            'case"thinking_delta"',
            'type==="stream_request_start"',
            "content_block_start",
        ),
    ),
]
