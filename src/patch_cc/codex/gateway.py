"""The localhost bridge: Claude Code's Anthropic requests -> Codex, and back.

A stateless HTTP server on 127.0.0.1. The redirect patch sends *only* Codex-model
requests here; each is translated to an OpenAI Responses call over plain HTTPS +
SSE (no WebSocket) and streamed back as Anthropic SSE. The server holds your
OpenAI token, and that is all it holds: a diverted request already names the
OpenAI model to run, so there is no registry here to consult and nothing that
could fall out of step with the bundle. Reasoning effort arrives the same way,
per request (Claude Code's live ``/effort``). It never sees an Anthropic-model
request.

The port is the one thing that has to agree with the binary, and the binary is
where it is read from -- ``serve`` asks the manifest, not a file of its own.

Nothing here is per-session state either: context is re-sent each turn, and
OpenAI's own ``prompt_cache_key`` carries the prefix cache. Claude Code does
re-send it -- once it has been told this server keeps nothing. Its newest
first-party betas assume a server that remembers the conversation, and it turns
them on for any model it believes it is sending to Anthropic, which a diverted
Codex model is; the gateway refuses those the way the API refuses a beta it does
not know, and Claude Code's own fallback resends the turn whole
(:data:`_STATEFUL_BETAS`). That is the whole reason this file is short.
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import BACKEND_HOST, BACKEND_UA, oauth, translate

_CODEX_PATH = "/backend-api/codex/responses"
#: Identifies the client to the Codex backend (free-form; the reference tool
#: sends its own name here too).
_ORIGINATOR = "patch-cc"
_UPSTREAM_TIMEOUT = 600

#: Every way a turn can end early: the upstream said it failed, or the upstream
#: leg failed underneath the protocol -- a dropped socket, a stall against
#: ``_UPSTREAM_TIMEOUT``, a truncated chunk. All of them are a finished turn to
#: report, never a traceback and never a stream that simply stops.
_TURN_FAILED = (translate.ResponsesError, OSError, http.client.HTTPException)

#: The ``anthropic-beta`` values this gateway refuses, by name prefix -- the date
#: suffix is a beta's version, the name is the feature. Claude Code sends them
#: only to what it takes for the first-party API, and a diverted Codex request
#: qualifies: the redirect rewrites the URL after every such decision is made.
#: Accepted by a server that keeps no conversation, they lose it:
#:
#: * **Message Threads** (``message-threads-``): the first request creates a
#:   thread carrying everything, and every later one is ``thread: {type:
#:   "continue", previous_message_id}`` with only the messages since that
#:   reply. Measured on 2.1.285 against a gateway that accepted it: the third
#:   turn arrived as one user message and a 49-character system message, and
#:   the ~1k-token turns in a user's transcript -- the model "not seeing" the
#:   conversation -- were exactly this.
#: * **Mid-conversation system** (``mid-conversation-system-``, one prefix for
#:   the beta and its ``-clear-at-`` sibling): the system prompt's dynamic half
#:   rides as ``role: "system"`` messages, a reminder sent with ``clear_at`` is
#:   sent once and kept server-side until then, and the same family carries
#:   tool changes inside messages. Only ``clear_at`` needs memory; the rest is
#:   a wire shape this translator was not written for, and the client's
#:   fallback for the whole family is the classic one it was.
#:
#: The refusal is the one answer Claude Code has a fallback for: it drops the
#: beta, resends the same turn stateless -- whole history, system prompt, tools
#: -- and stops asking, for the session (threads, kept reminders) or the
#: conversation (mid-conversation system). One beta per retry, so a session's
#: first Codex request costs three local round-trips, none of which reaches
#: OpenAI. The price is named: the switch is process-wide, so those features
#: stay off for the Claude-model requests of that session too.
_STATEFUL_BETAS = ("message-threads-", "mid-conversation-system-")


def stateful_betas(header: str | None) -> list[str]:
    """The values of an ``anthropic-beta`` header that :data:`_STATEFUL_BETAS` name."""
    return [
        value
        for value in (value.strip() for value in (header or "").split(","))
        if value.startswith(_STATEFUL_BETAS)
    ]


def unknown_beta(values: list[str]) -> str:
    """The API's own refusal of betas it does not know, for ``values``.

    The wording is the contract. Claude Code recognises a refused beta by the
    header's name and the value both appearing in the message, and the
    clear-at one also by this exact prefix on the message's first line; any
    other 400 is shown to the user as an error, with the beta sent again next
    turn.
    """
    listed = ", ".join(f"`{value}`" for value in values)
    return (
        f"Unexpected value(s) {listed} for the anthropic-beta header. Please "
        "consult our documentation at https://docs.anthropic.com/en/api/versioning"
    )


class Gateway:
    """A token and one upstream call. Deliberately holds no request state.

    There is no model registry here on purpose. The bundle diverts a request
    already naming the OpenAI model to run, so routing is identity: whatever a
    model-resolution table here said could only ever disagree with the binary that
    did the diverting, and a model chosen after this process started would be
    unroutable until it restarted.
    """

    def __init__(self) -> None:
        self._token_lock = threading.Lock()

    def _token(self) -> tuple[str, str | None]:
        # Load *and* refresh under one lock. Reading creds outside it let two
        # concurrent subagent calls both snapshot the same expiring token and each
        # refresh -- and the second, replaying a rotated-out refresh token, 502s.
        # Re-reading inside the lock lets it see the first call's saved token.
        with self._token_lock:
            creds = oauth.load()
            if creds is None:
                raise oauth.OAuthError("not signed in; run `patch-cc codex login`")
            access, creds = oauth.valid_access(creds)
        return access, creds.account_id

    def open_responses(self, payload: dict) -> http.client.HTTPResponse:
        """POST the translated payload to the Codex backend, streaming the reply."""
        access, account_id = self._token()
        headers = {
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "originator": _ORIGINATOR,
            "User-Agent": BACKEND_UA,
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        connection = http.client.HTTPSConnection(
            BACKEND_HOST, timeout=_UPSTREAM_TIMEOUT
        )
        connection.request(
            "POST", _CODEX_PATH, body=json.dumps(payload), headers=headers
        )
        return connection.getresponse()


def iter_sse(fp) -> Iterator[dict]:
    """Yield each SSE event's parsed ``data:`` JSON from a byte stream.

    The event name is ignored: every Responses frame carries its ``type`` inside
    the JSON, so that one field drives translation.
    """
    buffer: list[bytes] = []
    while True:
        line = fp.readline()
        if not line:
            break
        line = line.rstrip(b"\r\n")
        if not line:
            if buffer:
                try:
                    yield json.loads(b"".join(buffer))
                except ValueError:
                    pass
                buffer = []
            continue
        if line.startswith(b"data:"):
            buffer.append(line[5:].lstrip())


#: Flat character cost charged for one media block (~1.5k vision tokens x ~4),
#: so a screenshot's or a PDF's inline base64 -- which the translator ships as a
#: real vision/file part, not text -- is not counted at its ~40x-larger encoded
#: length and made to trip auto-compaction early.
_MEDIA_CHARS = 6000


def estimate_tokens(body: dict) -> int:
    """A cheap ~4-chars/token estimate for ``count_tokens``.

    Claude Code diverts count_tokens for a Codex model here too; OpenAI exposes
    no counting endpoint, so this keeps the context meter roughly honest rather
    than erroring. A media block (image or document) is charged a flat cost, not
    its base64 length: the translator lifts it out as a vision/file part shipping
    none of that base64 as text, so counting the base64 here made this and the
    request disagree by ~350k tokens on a 1 MB PDF and compact the session early.
    """
    chars = len(json.dumps(body.get("system", ""))) + len(
        json.dumps(body.get("tools", []))
    )
    chars += _content_chars(body.get("messages", []))
    return max(1, chars // 4)


def _content_chars(value: object) -> int:
    """Characters in a message tree, each media block counted flat, not by base64.

    A block is media when it carries a ``source`` -- an image or a document --
    the same test the translator lifts on, so the two agree on what a block
    costs. A multi-page document is only roughly served by one flat cost, but
    that is the honest unknown; its base64 length is a known wrong answer.
    """
    if isinstance(value, dict):
        if value.get("source") is not None:
            return _MEDIA_CHARS
        return sum(_content_chars(v) for v in value.values())
    if isinstance(value, list):
        return sum(_content_chars(v) for v in value)
    if isinstance(value, str):
        return len(value)
    return 0


class _Handler(BaseHTTPRequestHandler):
    gateway: Gateway  # set by serve()
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args) -> None:  # keep the terminal clean
        pass

    def do_POST(self) -> None:
        # One request per connection: an SSE reply is open-ended (no
        # Content-Length), so the stream ends by connection close.
        self.close_connection = True
        # This localhost bridge holds your OpenAI token and has no browser client:
        # Claude Code and curl send no Origin, so any Origin at all is a webpage
        # trying to spend your quota. Refusing the header's *presence* needs no
        # allowlist to keep current -- and no prefix test, which a host like
        # `127.0.0.1.example.com` walks straight through.
        if self.headers.get("Origin"):
            return self._error(403, "permission_error", "cross-site request refused")
        path = self.path.split("?", 1)[0]
        raw_length = self.headers.get("Content-Length") or "0"
        length = int(raw_length) if raw_length.isdigit() else 0
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
            if not isinstance(body, dict):
                # A valid-but-not-object body (`[]`, `5`, `null`) is malformed for
                # this API just as unparseable bytes are, and is answered the same
                # way. Reaching `_messages` with one used to raise straight out of
                # the handler thread: a traceback, a reset connection, no reply.
                raise ValueError(  # noqa: TRY004 - lands in json's own handler below
                    "not a JSON object"
                )
        except ValueError:
            return self._error(
                400, "invalid_request_error", "request body must be a JSON object"
            )
        if path == "/v1/messages":
            return self._messages(body)
        if path == "/v1/messages/count_tokens":
            return self._json(200, {"input_tokens": estimate_tokens(body)})
        return self._error(404, "not_found_error", f"no route for {path}")

    def _messages(self, body: dict) -> None:
        # The model named in the body *is* the OpenAI model to run: the bundle
        # registers Codex models under their own ids, and its resolver has already
        # rewritten any shortcut to one. So there is nothing to look up -- only the
        # body's own claim to check, since anything that reaches this port
        # unannounced is not a Claude Code turn.
        model = body.get("model")
        if not isinstance(model, str) or not model:
            return self._error(
                400, "invalid_request_error", "request body must name a model"
            )

        # A beta that means "remember this conversation for me" cannot be
        # honoured here, and accepting it anyway is how a turn arrives carrying
        # only its newest message. Refused before anything is translated, in
        # the words the client's fallback listens for (`_STATEFUL_BETAS`).
        refused = stateful_betas(self.headers.get("anthropic-beta"))
        if refused:
            return self._error(400, "invalid_request_error", unknown_beta(refused))

        # A tool the request *requires* but the backend cannot run is refused
        # here, not silently dropped: dropping it left the model answering a
        # forced WebSearch from memory. A 400 does not read as retryable, so the
        # SDK surfaces it instead of re-sending the doomed turn.
        refusal = translate.refused_tool_reason(body)
        if refusal is not None:
            return self._error(400, "invalid_request_error", refusal)

        session = self.headers.get("X-Claude-Code-Session-Id")
        payload = translate.translate_request(
            body, upstream_model=model, session_id=session
        )
        try:
            upstream = self.gateway.open_responses(payload)
            # A reasoning effort the model does not run is refused with a
            # structured 400 before any generation, so retrying costs one
            # round-trip. Clamping is the gateway's job and nobody else's:
            # which levels a model accepts is the backend's per-model ruling,
            # so the binary keeps offering its usual ladder (upstream's own
            # permissive fallback for models it does not know) and the refusal
            # itself -- never a baked table that could go stale -- decides.
            # `clamp_effort` steps one rung per pass and returns None for any
            # other failure, so this loop terminates on ladder length.
            while upstream.status != 200:
                raw = upstream.read().decode("utf8", "replace")
                upstream.close()
                clamped = translate.clamp_effort(payload, _upstream_error(raw))
                if clamped is None:
                    return self._error(
                        upstream.status,
                        translate_error_type(upstream.status),
                        raw[:500],
                    )
                payload = clamped
                upstream = self.gateway.open_responses(payload)
        except oauth.OAuthError as exc:
            # A sign-in failure -- not signed in, or a rotated-out session -- is
            # authentication, not a transient upstream fault. A 502 reads as
            # retryable, so the SDK re-uploaded the whole prompt against a token
            # that will keep failing; 401 says stop and fix the credential.
            return self._error(401, "authentication_error", str(exc))
        except _TURN_FAILED as exc:
            return self._error(502, "api_error", str(exc))

        try:
            conv = translate.ResponsesToAnthropic(
                response_model_id=model,
                message_id=f"msg_{uuid.uuid4().hex}",
                tool_required=translate.tool_required_props(body),
                input_estimate=estimate_tokens(body),
            )
            if body.get("stream"):
                self._stream(upstream, conv)
            else:
                self._collect(upstream, conv)
        finally:
            upstream.close()

    @staticmethod
    def _drain(upstream, conv: translate.ResponsesToAnthropic) -> Iterator[dict]:
        """Feed the upstream stream through ``conv``, yielding Anthropic events.

        EOF before a terminal frame is a dropped upstream, not a finished turn,
        and it raises here so both callers report it through the failure handling
        they already have. Left to each of them, only the collector ever grew the
        check: ``iter_sse`` returns cleanly at EOF, so the streaming half fell out
        of its loop and returned, sending no ``message_stop`` and no error --
        exactly the turn-that-never-ends its own comment warns about.
        """
        for event in iter_sse(upstream):
            yield from conv.feed(event)
        if not conv.completed:
            raise ConnectionError("upstream stream ended early")

    def _stream(self, upstream, conv: translate.ResponsesToAnthropic) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for out in self._drain(upstream, conv):
                self._sse(out)
        except _TURN_FAILED as exc:
            # Say what happened, in the protocol already in flight: a stream that
            # just stops leaves Claude Code waiting on a turn that will never end.
            # The write is best-effort -- if Claude Code is the side that hung up,
            # there is no one left to tell.
            with suppress(*_TURN_FAILED):
                self._sse(_error_event(exc))

    def _collect(self, upstream, conv: translate.ResponsesToAnthropic) -> None:
        try:
            for _ in self._drain(upstream, conv):
                pass
        except _TURN_FAILED as exc:
            return self._json(_status_for(exc), _error_event(exc))
        self._json(200, conv.message())

    # -- response helpers ----------------------------------------------------

    def _sse(self, event: dict) -> None:
        frame = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        self.wfile.write(frame)
        self.wfile.flush()

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, kind: str, message: str) -> None:
        self._json(
            status, {"type": "error", "error": {"type": kind, "message": message}}
        )


def _upstream_error(body: str) -> dict:
    """The upstream error object out of a non-200 body, or ``{}``.

    ``{}`` for unparseable bodies too: `clamp_effort` reads fields off it, and a
    body that is not the structured refusal simply matches nothing.
    """
    try:
        error = json.loads(body).get("error")
    except (ValueError, AttributeError):
        return {}
    return error if isinstance(error, dict) else {}


def translate_error_type(status: int) -> str:
    """Map an HTTP status to Anthropic's error ``type`` string."""
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
        529: "overloaded_error",
    }.get(status, "api_error")


def _status_for(exc: BaseException) -> int:
    """The status an ended turn should surface as, read off the upstream's code.

    A transport failure carries no code and lands on the 502 default, which is
    exactly what a dropped or stalled upstream is.
    """
    code = str(getattr(exc, "code", "") or "").lower()
    if "rate" in code or "quota" in code or "429" in code:
        return 429
    if "auth" in code or "401" in code:
        return 401
    # A client error -- an oversized or malformed prompt, or a refusal -- must not
    # read as a retryable 502, or the SDK re-sends the same doomed request in a
    # loop. `filter` is here because a content-filtered turn is the most certain
    # of all to fail again: nothing about resending it changes the verdict.
    if (
        "context" in code
        or "length" in code
        or "invalid" in code
        or "filter" in code
        or "400" in code
    ):
        return 400
    return 502


def _error_event(exc: BaseException) -> dict:
    status = _status_for(exc)
    return {
        "type": "error",
        "error": {
            "type": translate_error_type(status),
            # A transport failure can carry an empty message; its class name is
            # the only thing left that says anything.
            "message": str(exc) or exc.__class__.__name__,
        },
    }


def running(port: int) -> bool:
    """Is a gateway listening on ``port`` right now?

    Lives beside :func:`serve` because it is the same question from the other
    side, and every surface that names the port has to answer it: a patched
    binary routes Codex requests there whether or not anything is home, and a URL
    that leads nowhere reads exactly like one that works. Loopback, so the
    connect either succeeds or is refused at once; the timeout is only there so
    nothing can hang on it.
    """
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def serve(*, port: int, on_ready: Callable[[], object] = lambda: None) -> None:
    """Run the gateway on ``port`` until interrupted (foreground).

    The port is the whole of its configuration, so choosing a Codex model while
    this runs needs no restart -- there is nothing here that knows the set. A
    *re-bake* onto a different port does need one, which is why the apply report
    says so.

    ``on_ready`` fires once the socket is bound, so a caller announces a gateway
    that is actually listening. Announcing before the bind printed a green tick
    and then the address-in-use failure underneath it.
    """
    _Handler.gateway = Gateway()
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    on_ready()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
