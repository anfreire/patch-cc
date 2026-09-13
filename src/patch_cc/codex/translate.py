"""Pure translation between the Anthropic Messages API and the OpenAI Responses API.

This module is deliberately transport-free: dict in, dict out. The gateway owns
sockets, auth, and SSE framing; everything here is testable without a network.

Direction and shape are fixed by what Claude Code speaks (Anthropic Messages,
SSE) and what the ChatGPT/Codex OAuth backend speaks (OpenAI Responses over
plain POST + SSE). We translate straight between the two -- no intermediate SDK
representation -- which is why this file is small enough to read in one sitting.

The awkward parts are not ours; they are the ChatGPT Codex backend's, and each
is load-bearing (a naive mapping silently corrupts a session or balloons its
token cost). They are called out at the site that handles them:

* the backend rejects ``system`` -- it wants ``instructions`` -- and rejects an
  explicit output-token cap, and only ever answers as a stream;
* a media block (an image, or a ``document`` a PDF ``Read`` emits) left inside
  ``tool_result`` content is JSON-stringified into the function output and
  tokenized as *text* (a screenshot -> 200k+ tokens), so any block carrying a
  ``source`` is lifted out into the following user turn as a real vision/file
  part -- dispatched on the ``source``, never the block's tag, so a new media
  type rides the same lift instead of falling through;
* GPT-family models emit filler (top-level ``null``, empty arrays) that Claude
  Code forwards verbatim into server-side tool configs where an empty list is a
  400 -- so filler is stripped, except an empty array on a *required* property,
  where the emptiness is the intended value (``TodoWrite.todos: []`` clears);
* reasoning must round-trip: the backend runs ``store:false``, so the encrypted
  reasoning blob comes back as an Anthropic ``thinking.signature`` and is
  re-sent next turn -- the proxy, not the server, owns conversation state;
* reasoning summaries are opt-in: the backend streams ``reasoning.summary`` text
  only when the request asks for it, so codex otherwise reasons invisibly -- we
  request it unless Claude Code disables ``thinking`` and turn the summary deltas
  back into Anthropic ``thinking``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

from . import EFFORT_LADDER

#: The Codex OAuth backend rejects an empty/absent instruction set.
DEFAULT_SYSTEM = "You are a coding assistant."

#: Claude Code prepends this one line to the system prompt for billing
#: attribution; its token changes every request, so forwarding it would churn
#: the stable prompt prefix and defeat OpenAI's prefix cache.
_BILLING_PREFIX = "x-anthropic-billing-header:"


class ResponsesError(Exception):
    """An upstream frame saying the turn will not produce a usable answer.

    ``error``, ``response.failed``, and every ``response.incomplete`` whose reason
    is *not* ``max_output_tokens``. A length cap is the one incomplete that still
    carries a real partial answer, so it finalizes as a completion with a
    ``max_tokens`` stop reason instead; a content filter does not, and reporting
    it as an ordinary short answer hid a refusal as a result.

    Carries the raw event so the gateway can derive an HTTP status and an
    Anthropic-shaped error body from ``error.code`` / ``error.message``, falling
    back to the incomplete reason when there is no error object.
    """

    def __init__(self, event: dict[str, Any]):
        self.event = event
        err = _failure_error(event)
        # An `incomplete` frame carries no `error` object, only a reason. Without
        # it the one thing the user needs -- *why* the turn stopped -- was
        # replaced by "upstream request failed", and the status derived from a
        # code that was not there.
        reason = _incomplete_reason(event)
        super().__init__(
            err.get("message")
            or (f"upstream stopped early ({reason})" if reason else None)
            or "upstream request failed"
        )

    @property
    def code(self) -> str | None:
        code = _failure_error(self.event).get("code") or _incomplete_reason(self.event)
        return str(code) if code is not None else None


# ── Request: Anthropic Messages body -> Responses payload ────────────────────


def translate_request(
    body: dict[str, Any],
    *,
    upstream_model: str,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Build the ``response.create`` payload from an Anthropic ``/v1/messages`` body.

    ``upstream_model`` is the Codex id to run -- the same id the request named,
    since a Codex model has no second name. The reasoning effort is not part of a
    model's identity either; it rides in the request body -- Claude Code stamps the
    live ``/effort`` level into ``output_config.effort`` -- so it is read from
    ``body`` here. ``session_id`` lets the gateway pass Claude Code's session
    header so the prompt-cache key stays stable across a session's turns.
    """
    payload: dict[str, Any] = {
        "model": upstream_model,
        # `system` is refused by the Codex backend; it wants `instructions`.
        "instructions": _system_text(body.get("system")) or DEFAULT_SYSTEM,
        "input": _translate_messages(body.get("messages") or []),
        # The proxy owns conversation state and re-sends context each turn, so
        # the server must not persist it; the encrypted reasoning blob is what we
        # round-trip instead (see `include`).
        "store": False,
        "include": ["reasoning.encrypted_content"],
        # The Codex backend only ever answers as a stream, whatever the client
        # asked for; the gateway collapses it back for a non-streaming client.
        "stream": True,
    }

    tools = _translate_tools(body.get("tools"))
    if tools:
        payload["tools"] = tools
        choice = _translate_tool_choice(body.get("tool_choice"))
        if choice is not None:
            payload["tool_choice"] = choice

    # Reasoning effort and summary both ride on the `reasoning` block. Effort is
    # the live `/effort`; the summary is opt-in -- the Codex backend streams
    # `reasoning.summary` text only when asked, so without this codex reasons
    # invisibly. We mirror Claude Code's own `thinking` display intent into it
    # (verified live: `summary:"auto"` -> `reasoning_summary_text.delta`, which
    # the response half turns back into Anthropic `thinking`).
    mapped = _map_effort(_body_effort(body))
    want_summary = _wants_reasoning_summary(body)
    if mapped or want_summary:
        reasoning: dict[str, Any] = {}
        if mapped:
            reasoning["effort"] = mapped
        if want_summary:
            reasoning["summary"] = "auto"
        payload["reasoning"] = reasoning

    key = cache_key(body, session_id)
    if key:
        payload["prompt_cache_key"] = key

    # An explicit output cap makes the Codex backend return an empty
    # finish:"other" response, so `max_tokens` is intentionally dropped.
    if body.get("temperature") is not None:
        payload["temperature"] = body["temperature"]
    return payload


def tool_required_props(body: dict[str, Any]) -> dict[str, set[str]]:
    """``tool name -> required property names``, for output-time filler stripping."""
    required: dict[str, set[str]] = {}
    for tool in body.get("tools") or []:
        name = tool.get("name")
        if name:
            schema = tool.get("input_schema") or {}
            required[name] = set(schema.get("required") or [])
    return required


def _system_text(system: Any) -> str:
    """Flatten Anthropic ``system`` to text, dropping the volatile billing line."""
    if system is None:
        return ""
    if isinstance(system, str):
        text = system
    else:
        parts = [
            blk if isinstance(blk, str) else (blk.get("text") or "")
            for blk in system
            if isinstance(blk, (str, dict))
        ]
        text = "\n".join(parts)
    if text.startswith(_BILLING_PREFIX):
        newline = text.find("\n")
        text = "" if newline == -1 else text[newline + 1 :]
    return text.strip()


def _translate_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        elif content is None:
            content = []
        if message.get("role") == "assistant":
            out.extend(_assistant_items(content))
        else:
            _user_items(content, out)
    return out


#: Codex ``encrypted_content`` is a Fernet token (version byte ``0x80`` ->
#: base64url ``"gAAAA..."``). An Anthropic ``thinking.signature`` is base64-std
#: protobuf and starts with ``E``, never ``g`` -- so this prefix cleanly tells a
#: resumable Codex reasoning blob from a foreign signature.
_CODEX_REASONING_PREFIX = "gAAAA"


def _is_codex_reasoning(signature: Any) -> bool:
    """Whether ``signature`` is a Codex blob we can re-send as reasoning state.

    Only a genuine Fernet blob is resumable state for the Codex backend. A
    foreign Anthropic ``thinking.signature`` -- which rides in when a session is
    ``/model``-switched between a Claude model and a Codex one -- is
    undecryptable here and carries no Codex reasoning, so it is treated as
    display-only, exactly like a signature-less block.
    """
    return isinstance(signature, str) and signature.startswith(_CODEX_REASONING_PREFIX)


def _assistant_items(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Assistant blocks -> Responses output items, preserving order.

    Consecutive text blocks fold into one ``message`` item; a reasoning or tool
    block flushes the pending text first so interleaving (reason -> speak -> call)
    survives the round-trip.
    """
    items: list[dict[str, Any]] = []
    text: list[str] = []

    def flush_text() -> None:
        if text:
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "".join(text)}],
                }
            )
            text.clear()

    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            text.append(block.get("text") or "")
        elif kind == "thinking":
            flush_text()
            signature = block.get("signature")
            thinking = block.get("thinking") or ""
            # A reasoning item is only worth re-sending if it carries a Codex
            # encrypted blob: under store:false that blob *is* the reasoning
            # state. A signature-less block is display-only summary text the model
            # cannot resume from (re-send waste); a *foreign* Anthropic signature
            # (from a cross-model /model switch) is undecryptable to the backend.
            # Keep only a genuine Fernet blob and drop the rest. Claude Code
            # already strips cross-model thinking before it reaches us, so this
            # is defense-in-depth: the gateway owns its own contract.
            if not _is_codex_reasoning(signature):
                continue
            items.append(
                {
                    "type": "reasoning",
                    "summary": [{"type": "summary_text", "text": thinking}]
                    if thinking
                    else [],
                    "encrypted_content": signature,
                }
            )
        elif kind == "tool_use":
            flush_text()
            items.append(
                {
                    "type": "function_call",
                    "call_id": block.get("id") or "",
                    "name": block.get("name") or "",
                    "arguments": json.dumps(
                        block.get("input") or {}, separators=(",", ":")
                    ),
                }
            )
    flush_text()
    return items


def _user_items(blocks: list[dict[str, Any]], out: list[dict[str, Any]]) -> None:
    """User blocks -> Responses items: tool outputs first, then the user message.

    Images inside a tool_result are lifted into the user message that follows the
    outputs, as real vision parts, so their base64 never lands in output text.
    """
    lifted: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "text":
            parts.append({"type": "input_text", "text": block.get("text") or ""})
        elif block.get("source") is not None:
            # Any block carrying a `source` -- an image, or a `document` (a PDF
            # `Read` emits) -- rides the media lift, keyed on media_type. Keyed on
            # the tag instead, a document fell off the end and was dropped.
            parts.append(_media_part(block.get("source")))
        elif kind == "tool_result":
            output, images = _tool_result_output(block)
            if block.get("is_error"):
                # A Responses function output has no error flag, so mark it
                # in-band; otherwise a failed Bash/Edit reads to the model as an
                # ordinary success (the image-lift note takes the same approach).
                output = "[tool error] " + output
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": block.get("tool_use_id") or "",
                    "output": output,
                }
            )
            lifted.extend(images)
    content = lifted + parts
    if content:
        out.append({"type": "message", "role": "user", "content": content})


def _tool_result_output(block: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Serialize a tool_result's content, lifting any images out for the caller.

    Returns ``(output_string, lifted_parts)`` where each lifted image becomes a
    label ``input_text`` plus an ``input_image`` for the following user message,
    and the inline block is replaced by a light note so the model still sees
    where the image belongs.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content, []
    if content is None:
        return "", []

    tool_use_id = block.get("tool_use_id") or ""
    serialized: list[Any] = []
    lifted: list[dict[str, Any]] = []
    n = 0
    for part in content:
        if not isinstance(part, dict):
            serialized.append(part)
            continue
        if part.get("source") is not None:
            # An image *or* a document: lifted out as a real vision/file part, so
            # its base64 never lands in the function output. A document reached
            # the `else` below and was JSON-stringified into the output and
            # tokenized as text -- a 1 MB PDF as ~350k tokens, the very hazard
            # this lift exists to prevent.
            n += 1
            media = (part.get("source") or {}).get("media_type") or ""
            noun = "image" if media.startswith("image/") else "file"
            lifted.append(
                {
                    "type": "input_text",
                    "text": f"The following {noun} is {noun} {n} of tool call {tool_use_id}:",
                }
            )
            lifted.append(_media_part(part.get("source")))
            serialized.append(
                {
                    "type": part.get("type") or noun,
                    "note": f"attached to the next user message as {noun} {n} of tool call {tool_use_id}",
                }
            )
        elif part.get("type") == "text":
            serialized.append({"type": "text", "text": part.get("text") or ""})
        else:
            serialized.append(part)
    return json.dumps(serialized, separators=(",", ":")), lifted


def _media_part(source: Any) -> dict[str, Any]:
    """A Responses vision/file part for any block carrying a ``source``.

    Dispatched on the source's ``media_type``, not the block's tag, so a
    ``document`` (a PDF ``Read`` emits) becomes a real ``input_file`` instead of
    being dropped or stringified as text. An image stays ``input_image``; any
    other media type is sent as ``input_file`` carrying the same base64 data URL.

    The Codex backend's acceptance of ``input_file`` is not verifiable here (no
    credentials), but a clean 400 for a file it will not take beats a screenshot
    tokenized as 350k tokens of text or a page the model never sees.
    """
    source = source or {}
    if source.get("type") == "url":
        return {"type": "input_image", "image_url": source.get("url") or ""}
    media = source.get("media_type") or "image/png"
    data_url = f"data:{media};base64,{source.get('data') or ''}"
    if media.startswith("image/"):
        return {"type": "input_image", "image_url": data_url}
    subtype = media.rsplit("/", 1)[-1] or "bin"
    return {
        "type": "input_file",
        "filename": f"attachment.{subtype}",
        "file_data": data_url,
    }


def _without_pattern(schema: Any) -> Any:
    """The same schema with every ``pattern`` gone, at any depth.

    The backend validates each ``pattern`` as a regex of its own dialect and
    refuses the whole request when one does not parse -- ``Invalid schema for
    function 'X': '...' is not a 'regex'``, an ``invalid_function_parameters``
    400 that kills the turn before the model is reached, for a tool the model
    was never going to call. Claude Code ships one: the Artifact tool's
    ``^(?!__.*__$)[^\\p{Cc}\\p{Cf}\\p{Zl}\\p{Zp}"\\\\./[\\]]{1,200}$``. Measured
    against the live backend, a lookahead alone is accepted and a ``\\p{...}``
    class alone is accepted; together they are refused -- the Unicode class
    appears to pick an engine that has no lookaround, and there is no reason to
    think that is the only such pair.

    So this drops the key rather than testing the regex: with ``strict`` false
    the backend enforces none of them anyway, which makes ``pattern`` a hint the
    model reads in the schema text either way -- and dropping it uniformly costs
    a hint no one was owed, while keeping it costs whole turns on a dialect we
    would have to track upstream of two vendors at once.
    """
    if isinstance(schema, dict):
        return {k: _without_pattern(v) for k, v in schema.items() if k != "pattern"}
    if isinstance(schema, list):
        return [_without_pattern(item) for item in schema]
    return schema


def _translate_tools(tools: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for tool in tools or []:
        name = tool.get("name")
        schema = tool.get("input_schema")
        if not name or schema is None:
            continue
        out.append(
            {
                "type": "function",
                "name": name,
                "description": tool.get("description") or "",
                "parameters": _without_pattern(schema),
                "strict": False,
            }
        )
    return out


def refused_tool_reason(body: dict[str, Any]) -> str | None:
    """Why this request cannot be honestly served, if a *required* tool is missing.

    A server-side tool (WebSearch, ``/advisor``) has no ``input_schema`` and is
    not a function the Responses backend can call, so :func:`_translate_tools`
    drops it. When the request merely *offers* it that is a fair degrade -- the
    model codes on without it. But when ``tool_choice`` *requires* it, dropping
    it (and, with the ``if tools:`` gate, the ``tool_choice`` too) left the model
    a bare "Perform a web search for: X" and an answer from memory rendered as a
    web result. The honest translation of "the backend cannot run the tool you
    require" is a refusal, so the gateway answers 400 ``invalid_request_error``
    -- which :func:`patch_cc.codex.gateway._status_for` already keeps the SDK
    from retrying -- rather than a confident wrong answer.
    """
    choice = body.get("tool_choice")
    if not isinstance(choice, dict):
        return None
    supported = {tool["name"] for tool in _translate_tools(body.get("tools"))}
    kind = choice.get("type")
    if kind == "tool" and (name := choice.get("name")) and name not in supported:
        return f"the required tool `{name}` is not one the Codex backend can run"
    if kind == "any" and body.get("tools") and not supported:
        return "this request requires a tool, but none of its tools can run on the Codex backend"
    return None


def _translate_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, dict):
        return None
    kind = choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool" and choice.get("name"):
        return {"type": "function", "name": choice["name"]}
    return None


def _body_effort(body: dict[str, Any]) -> str | None:
    """The reasoning effort Claude Code stamped on this request.

    Claude Code carries the live ``/effort`` level in ``output_config.effort`` --
    a plain string sent on every effort-capable request, alongside ``thinking``
    and any structured-output ``format`` in the same object. That, not anything
    baked into the model's identity, drives codex reasoning per turn. When absent
    (a request that carries no effort) the backend default stands.
    """
    config = body.get("output_config")
    effort = config.get("effort") if isinstance(config, dict) else None
    return effort if isinstance(effort, str) else None


def _wants_reasoning_summary(body: dict[str, Any]) -> bool:
    """Whether to ask the Codex backend to stream its reasoning summary.

    Mirrors Claude Code's *own* display predicate: the binary enables thinking on
    ``thinking.type != "disabled"`` (its step-display code), so we request
    ``reasoning.summary`` on exactly that condition. Anything other than an
    explicit ``{"type":"disabled"}`` counts as on -- a real turn carries
    ``{"type":"adaptive",...}``, and a bare ``{}`` or an unknown positive type is
    treated as on too, matching the binary rather than second-guessing it. Only an
    explicit disable (internal structured-output/title-gen) opts out, keeping
    those calls free of summary text. Absent or non-dict ``thinking`` is the one
    "off" case -- fail-closed for a caller that is not Claude Code, which always
    sends the field on real turns. ``display`` is not consulted: the binary gates
    the *enable* decision on ``type`` alone.
    """
    thinking = body.get("thinking")
    return isinstance(thinking, dict) and thinking.get("type") != "disabled"


# EFFORT_LADDER is imported from the package's one home (see the top of this
# file): the clamp steps a Codex request one rung at a time against it. The
# backend speaks the same words (verified on the wire: xhigh and max both reach
# it and run), though not every model accepts every rung -- which is the clamp's
# whole subject. `minimal` (a Responses-API level Claude never emits) is not on
# the ladder: dropping the field falls back to the backend default, never a 400.


def _map_effort(effort: str | None) -> str | None:
    """Anthropic effort -> Responses reasoning effort.

    The Codex backend takes Claude's whole ladder, so a recognized level passes
    straight through; the earlier ``xhigh -> high`` clamp silently downgraded
    reasoning and dropped ``max``. An unrecognized value drops the field rather
    than 400. Whether the *model* runs the level is the backend's ruling, made
    per request and honoured by :func:`clamp_effort`.
    """
    if not effort:
        return None
    level = str(effort).strip().lower()
    return level if level in EFFORT_LADDER else None


def clamp_effort(
    payload: dict[str, Any], error: dict[str, Any]
) -> dict[str, Any] | None:
    """The payload one effort step lower, when ``error`` refused its level.

    A level the model does not run comes back as a structured 400 --
    ``param: "reasoning.effort"``, ``code: "invalid_value"`` (measured: gpt-5.5
    takes ``xhigh`` and 400s ``max``) -- *before* any generation, so retrying is
    cheap. Claude Code's own rule for its models is "fall back to the highest
    supported level at or below the one you set"; this mirrors it where the
    refusal happens: step down one rung and let the gateway retry, dropping the
    field entirely below ``low`` so the backend default stands. The menu can
    keep offering the whole ladder for a model the binary does not know -- that
    is upstream's own permissive fallback -- and a level it over-offered costs
    one extra round-trip instead of a dead turn.

    ``None`` means "not that failure, or nothing left to lower": a different
    param or code, or a payload already carrying no effort. Each call strictly
    lowers or removes the level, so the retry loop terminates on ladder length.
    """
    if error.get("param") != "reasoning.effort" or error.get("code") != "invalid_value":
        return None
    reasoning = payload.get("reasoning")
    current = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if not isinstance(current, str) or not isinstance(reasoning, dict):
        return None
    below = (
        EFFORT_LADDER[: EFFORT_LADDER.index(current)]
        if current in EFFORT_LADDER
        else ()
    )
    remaining = {key: value for key, value in reasoning.items() if key != "effort"}
    clamped = {key: value for key, value in payload.items() if key != "reasoning"}
    if below:
        clamped["reasoning"] = {**remaining, "effort": below[-1]}
    elif remaining:
        clamped["reasoning"] = remaining
    return clamped


def cache_key(body: dict[str, Any], session_id: str | None = None) -> str | None:
    """A prompt-cache key stable across a session's turns.

    Keyed on the session id when known, else on the *stable prefix* only -- the
    system prompt and tool definitions -- never the inline system-reminders,
    whose fresh timestamps would churn the key every turn and defeat grouping.
    """
    sid = session_id or _session_from_metadata(body)
    if sid:
        return "pcc-session-" + hashlib.sha256(sid.encode()).hexdigest()[:32]
    system = _system_text(body.get("system"))
    signature = "\x02".join(
        f"{tool.get('name', '')}\x01{tool.get('description', '')}\x01"
        f"{json.dumps(tool.get('input_schema') or {}, sort_keys=True, separators=(',', ':'))}"
        for tool in body.get("tools") or []
    )
    material = f"{system}\x00{signature}"
    return "pcc-" + hashlib.sha256(material.encode()).hexdigest()[:32]


def _session_from_metadata(body: dict[str, Any]) -> str | None:
    user_id = (body.get("metadata") or {}).get("user_id")
    if isinstance(user_id, str) and user_id:
        try:
            parsed = json.loads(user_id)
        except (ValueError, TypeError):
            return None
        if isinstance(parsed, dict) and isinstance(parsed.get("session_id"), str):
            return parsed["session_id"] or None
    return None


# ── Response: Responses SSE events -> Anthropic SSE events ───────────────────


def sanitize_tool_input(value: Any, required: set[str]) -> Any:
    """Strip GPT filler -- ``null`` and empty arrays -- but never a required key.

    Emptiness on a property the schema *requires* is the intended value, not
    filler (``TodoWrite.todos: []`` clears the list). That exception applied only
    to arrays, so a required nullable property was still dropped, turning the
    call into one missing a key the tool declares mandatory. One rule for both.
    """
    if not isinstance(value, dict):
        return value
    return {
        key: item
        for key, item in value.items()
        if key in required
        or not (item is None or (isinstance(item, list) and not item))
    }


class ResponsesToAnthropic:
    """Turn a Codex Responses event stream into Anthropic SSE, incrementally.

    Feed it parsed Responses events; :meth:`feed` yields Anthropic SSE event
    dicts (the gateway frames them). At most one Anthropic content block is open
    at a time -- Responses streams its output items sequentially, so a new item
    closes the previous block -- which keeps the block bookkeeping trivial.

    ``message_start`` is deliberately withheld until the first content: the
    Responses stream can fail before any output, and emitting a 200 ``message_start``
    first would mask that upstream error as an empty success.
    """

    def __init__(
        self,
        *,
        response_model_id: str,
        message_id: str = "msg_pcc",
        tool_required: dict[str, set[str]] | None = None,
    ):
        self.model_id = response_model_id
        self.message_id = message_id
        self.tool_required = tool_required or {}
        self._started = False
        self._index = -1
        self._open: str | None = None  # "text" | "thinking" | "tool"
        self._block: dict[str, Any] | None = None
        self._tool_name = ""
        self._tool_raw = ""
        self._tool_flushed = False
        self._pending_sig = ""
        self._finish = "end_turn"
        self._usage: dict[str, int] | None = None
        #: Set once a terminal upstream event arrives; the non-stream collector
        #: reads it to tell a finished turn from a silently dropped connection.
        self.completed = False
        self.blocks: list[dict[str, Any]] = []  # accumulated, for the non-stream result

    # -- lifecycle -----------------------------------------------------------

    def feed(self, event: dict[str, Any]) -> Iterator[dict[str, Any]]:
        kind = event.get("type")
        if kind == "response.output_item.added":
            yield from self._on_item_added(event.get("item") or {})
        elif kind == "response.output_text.delta":
            yield from self._on_text_delta(event.get("delta") or "")
        elif kind == "response.reasoning_summary_text.delta":
            yield from self._on_thinking_delta(event.get("delta") or "")
        elif kind == "response.function_call_arguments.delta":
            self._tool_raw += event.get("delta") or ""
        elif kind == "response.output_item.done":
            yield from self._on_item_done(event.get("item") or {})
        elif kind in ("response.completed", "response.incomplete"):
            # A length cap is a truncated-but-usable turn: finalize the partial
            # content and carry `max_tokens` as the stop reason rather than
            # raising. Every *other* incomplete reason -- a content filter, most
            # of all -- is the turn not happening, and finalizing it too reported
            # a refusal as an ordinary short answer that simply ended.
            reason = _incomplete_reason(event)
            if kind == "response.incomplete" and reason != "max_output_tokens":
                raise ResponsesError(event)
            self._usage = _extract_usage(event)
            if reason == "max_output_tokens":
                self._finish = "max_tokens"
            self.completed = True
            yield from self._finalize()
        elif kind in ("response.failed", "error"):
            raise ResponsesError(event)

    def message(self) -> dict[str, Any]:
        """The full Anthropic Messages object, for a non-streaming client."""
        return {
            "id": self.message_id,
            "type": "message",
            "role": "assistant",
            "model": self.model_id,
            "content": [b for b in self.blocks if _nonempty_block(b)],
            "stop_reason": self._finish,
            "stop_sequence": None,
            "usage": anthropic_usage(self._usage),
        }

    # -- event handlers ------------------------------------------------------

    def _on_item_added(self, item: dict[str, Any]) -> Iterator[dict[str, Any]]:
        kind = item.get("type")
        if kind == "reasoning":
            yield from self._open_block("thinking")
        elif kind == "message":
            yield from self._open_block("text")
        elif kind == "function_call":
            yield from self._open_block(
                "tool",
                tool_id=item.get("call_id") or item.get("id") or "",
                tool_name=item.get("name") or "",
            )

    def _on_text_delta(self, delta: str) -> Iterator[dict[str, Any]]:
        yield from self._ensure_block("text")
        self._block["text"] += delta  # type: ignore[index]
        yield _sse(
            "content_block_delta",
            index=self._index,
            delta={"type": "text_delta", "text": delta},
        )

    def _on_thinking_delta(self, delta: str) -> Iterator[dict[str, Any]]:
        yield from self._ensure_block("thinking")
        self._block["thinking"] += delta  # type: ignore[index]
        yield _sse(
            "content_block_delta",
            index=self._index,
            delta={"type": "thinking_delta", "thinking": delta},
        )

    def _on_item_done(self, item: dict[str, Any]) -> Iterator[dict[str, Any]]:
        kind = item.get("type")
        if kind == "reasoning":
            self._pending_sig = item.get("encrypted_content") or self._pending_sig
            yield from self._close_block()
        elif kind == "message":
            yield from self._close_block()
        elif kind == "function_call":
            self._finish = "tool_use"
            args = item.get("arguments")
            yield from self._flush_tool(self._tool_raw if args is None else args)
            yield from self._close_block()

    # -- block machinery -----------------------------------------------------

    def _open_block(
        self, kind: str, *, tool_id: str = "", tool_name: str = ""
    ) -> Iterator[dict[str, Any]]:
        # Closing first is what lets a still-open tool flush under its *own*
        # name: filler stripping looks the required properties up by name, so
        # adopting the incoming one any earlier would sanitize the previous
        # call's arguments against the next call's schema.
        yield from self._close_block()
        yield from self._ensure_started()
        self._index += 1
        self._open = kind
        self._tool_name = tool_name
        self._pending_sig = ""
        self._tool_raw = ""
        self._tool_flushed = False
        if kind == "text":
            self._block = {"type": "text", "text": ""}
        elif kind == "thinking":
            self._block = {"type": "thinking", "thinking": "", "signature": ""}
        else:
            self._block = {
                "type": "tool_use",
                "id": tool_id,
                "name": self._tool_name,
                "input": {},
            }
        self.blocks.append(self._block)
        yield _sse(
            "content_block_start", index=self._index, content_block=dict(self._block)
        )

    def _ensure_block(self, kind: str) -> Iterator[dict[str, Any]]:
        if self._open != kind:
            yield from self._open_block(kind)

    def _close_block(self) -> Iterator[dict[str, Any]]:
        if self._open is None:
            return
        if self._open == "thinking":
            self._block["signature"] = self._pending_sig  # type: ignore[index]
            yield _sse(
                "content_block_delta",
                index=self._index,
                delta={"type": "signature_delta", "signature": self._pending_sig},
            )
        elif self._open == "tool" and not self._tool_flushed:
            yield from self._flush_tool(self._tool_raw)
        yield _sse("content_block_stop", index=self._index)
        self._open = None
        self._block = None

    def _flush_tool(self, raw: str) -> Iterator[dict[str, Any]]:
        if self._tool_flushed:
            return
        self._tool_flushed = True
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = raw  # a non-JSON custom-tool input must still pass through
        sanitized = sanitize_tool_input(
            parsed, self.tool_required.get(self._tool_name, set())
        )
        if self._block is not None:
            self._block["input"] = sanitized
        yield _sse(
            "content_block_delta",
            index=self._index,
            delta={
                "type": "input_json_delta",
                "partial_json": json.dumps(sanitized, separators=(",", ":")),
            },
        )

    def _ensure_started(self) -> Iterator[dict[str, Any]]:
        if self._started:
            return
        self._started = True
        yield _sse(
            "message_start",
            message={
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": self.model_id,
                "stop_reason": None,
                "stop_sequence": None,
                # Zeroes here, since Responses reports usage only at completion.
                # Shaped by the one helper anyway, so the two events and the
                # non-streaming message can never describe usage differently.
                "usage": anthropic_usage(self._usage),
            },
        )

    def _finalize(self) -> Iterator[dict[str, Any]]:
        yield from self._close_block()
        yield from self._ensure_started()
        yield _sse(
            "message_delta",
            delta={"stop_reason": self._finish, "stop_sequence": None},
            # The whole count, not just the output half. Responses does not know
            # the input total until it completes, so `message_start` carried
            # zeroes and this is the only event that can say what a turn cost --
            # sending output alone left every streamed turn (which is every real
            # turn) reading as nothing in, nothing cached.
            usage=anthropic_usage(self._usage),
        )
        yield _sse("message_stop")


def _sse(event_type: str, **fields: Any) -> dict[str, Any]:
    return {"type": event_type, **fields}


def _nonempty_block(block: dict[str, Any]) -> bool:
    if block.get("type") == "text":
        return bool(block.get("text"))
    if block.get("type") == "thinking":
        return bool(block.get("thinking") or block.get("signature"))
    return True


def _response(event: dict[str, Any]) -> dict[str, Any]:
    response = event.get("response")
    return response if isinstance(response, dict) else {}


def _extract_usage(event: dict[str, Any]) -> dict[str, int] | None:
    usage = _response(event).get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}

    def num(value: Any) -> int:
        return value if isinstance(value, int) else 0

    return {
        "input_tokens": num(usage.get("input_tokens")),
        "cached_tokens": num(details.get("cached_tokens")),
        "cache_write_tokens": num(
            details.get("cache_write_tokens") or usage.get("cache_write_tokens")
        ),
        "output_tokens": num(usage.get("output_tokens")),
    }


def _incomplete_reason(event: dict[str, Any]) -> str | None:
    details = _response(event).get("incomplete_details")
    return details.get("reason") if isinstance(details, dict) else None


def anthropic_usage(usage: dict[str, int] | None) -> dict[str, int]:
    """Split the Responses total-input count into Anthropic's cache buckets.

    Responses reports ``input_tokens`` as the *total* including cache; Anthropic
    wants the fresh input alone plus the cache read/write split out.
    """
    usage = usage or {}
    cached = usage.get("cached_tokens", 0)
    written = usage.get("cache_write_tokens", 0)
    return {
        "input_tokens": max(0, usage.get("input_tokens", 0) - cached - written),
        "output_tokens": usage.get("output_tokens", 0),
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": cached,
    }


def _failure_error(event: dict[str, Any]) -> dict[str, Any]:
    for candidate in (event.get("error"), _response(event).get("error")):
        if isinstance(candidate, dict):
            return candidate
    return {}
