"""What a Codex model is, how we learn of one, and the handles derived from it.

An **id** is the whole of a model's identity: it is all ``--codex`` takes, all
the selection cache keeps, all the manifest records, and the key a diverted
request already carries to the gateway. Everything else about a model is
*derived*, never named:

============== ======================================================
fact           source
============== ======================================================
id             the argument
name           discovery, else the id itself
context window discovery, else ``0`` -- Claude Code's own 200k default
efforts        discovery, else ``()`` -- nothing is declared
shortcut       derived from the id (``sol``), never chosen
============== ======================================================

That is not a convention to remember but the reason the rest of the bridge is
small: with no second name for a model, there is no map anywhere that could
disagree with the bundle, so the manifest records ids alone and the gateway reads
nothing but the port.

Discovery reads every derived fact from the plan's own Codex backend and guesses
none. That endpoint is the *only* source: the general ChatGPT model list reports
different (non-Codex) context windows, and a community catalogue reports
different ones again, for models this plan may not even offer. A list we cannot
call is worse than no list -- it defers the failure to mid-session, where it
arrives as a translated 4xx instead of "couldn't reach your plan" at the moment
of asking. So an unreachable backend answers with nothing, and every caller
already says so.

The reasoning effort *level* is not part of a model's identity: it rides in
each request (Claude Code's live ``/effort``, read from ``output_config.effort``
in the gateway), so one chosen model serves every level. Which levels a model
*accepts* is a per-model fact the plan reports (``supported_reasoning_levels``),
carried here so the registry entry can declare them as capabilities
(:mod:`patch_cc.patches.codex`). The plan's *default* level is deliberately not
carried: inside Claude Code, "no effort chosen" already means the harness's own
default (``high``, the same one its flagships declare), and nothing here should
re-import Codex-the-product's UX. Nothing enforces the supported list
client-side either: the backend refuses a level it will not run, and the
gateway clamps off that refusal
(:func:`patch_cc.codex.translate.clamp_effort`) -- so a stale bake degrades to a
retry, never to a dead turn.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import BACKEND_HOST, BACKEND_UA, oauth

_BACKEND = f"https://{BACKEND_HOST}/backend-api"
_HTTP_TIMEOUT = 15


@dataclass(frozen=True, slots=True)
class CodexModel:
    """One Codex model as Claude Code will see it.

    Only :attr:`id` is required, because only :attr:`id` is ever *given*: a model
    named on the command line before the plan has been asked is a complete,
    bakeable model already. The two derived fields carry what discovery found, and
    their empty values are the honest fallbacks rather than placeholders -- which
    is why nothing here needs normalising at construction.
    """

    #: The OpenAI model id, e.g. ``gpt-5.6-sol``. The identity everywhere: the
    #: ``/model`` name, the subagent ``model:`` value, the routing key, and the
    #: model the gateway asks the backend to run.
    id: str
    #: Display label as discovery reported it; empty when it did not report one.
    name: str = ""
    #: Context window in tokens; ``0`` when unknown, which leaves Claude Code's
    #: own 200k default standing rather than baking a number we invented.
    context: int = 0
    #: Reasoning efforts the plan reports for this model, in the plan's own
    #: order; empty when it reported none (or could not be asked). Kept verbatim
    #: -- levels Claude Code never speaks (``ultra``) included -- because this is
    #: the plan's fact, and each consumer filters for its own vocabulary.
    efforts: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """The best name we have -- the id when the plan did not give one.

        A property rather than a value normalised at construction, so "we were
        never told this model's name" stays distinguishable from "its name is its
        id" at the one place that cares (:func:`patch_cc.cli` deciding whether the
        name adds a word), while every renderer gets a non-empty string for free.
        """
        return self.name or self.id


#: Short handles a derived family shortcut must never take, plus the menu's own
#: sentinels (``inherit``/``keep``/``default``). Its one irreducible job is the
#: path with no binary in hand: :func:`family_aliases` derives a shortcut from
#: the chosen ids alone, so a pathological ``gpt-5.6-opus`` cannot mint an
#: ``opus`` shortcut. For that it is a snapshot, and upstream outgrowing it costs
#: at most a shortcut that resolves nowhere. A *typed* ``--codex`` id is refused
#: against the bundle's own tables instead (`patches.codex.claimed_model_names`)
#: -- derived, and so unable to fall behind. docs/PLAYBOOK.md has the split.
_RESERVED = frozenset(
    {
        "sonnet",
        "opus",
        "haiku",
        "fable",
        "opusplan",
        "best",
        "default",
        "inherit",
        "keep",
    }
)

#: ``gpt-5.6-sol`` -> version ``5.6``, family ``sol``. The trailing word is the
#: shortcut (Claude's own ``opus``); the numeric version orders "latest" within a
#: family. Deliberately strict: a bare ``gpt-5.5`` or an off-shape id simply does
#: not match, and gets no shortcut.
_FAMILY_RE = re.compile(r"^gpt-(\d+(?:\.\d+)*)-([a-z]+)$")

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")


def family_of(model_id: str) -> str | None:
    """The family word of a clean ``gpt-<ver>-<family>`` id, else ``None``.

    The same parse :func:`family_aliases` orders "newest" with, exposed for the
    one other consumer (the registry entry's ``family`` field), so the two can
    never disagree about what counts as a family.
    """
    match = _FAMILY_RE.match(model_id)
    return match.group(2) if match else None


def is_valid_id(model_id: str) -> bool:
    """A safe binary identity: lowercase, no spaces, no shape-breaking chars.

    Only ``--codex`` needs asking -- every other id comes from the plan's own
    catalogue and is valid by construction. But that is exactly the path no plan
    can vouch for, because an offline ``apply`` bakes what you typed.

    ``fullmatch``, because ``$`` also matches *before* a trailing newline -- so
    ``"sol\\n"`` passed as valid and became the id baked into the bundle.
    """
    return bool(_ID_RE.fullmatch(model_id))


def is_reserved_id(model_id: str) -> bool:
    """Whether ``model_id`` would impersonate a Claude model or a menu sentinel.

    Registered under a native identity, a Codex model would divert *that* model's
    own requests to the gateway -- Anthropic prompts to OpenAI. Claude's ids are a
    disjoint namespace (``claude-*`` plus the short built-ins in
    :data:`_RESERVED`), so reserving them rejects no real OpenAI model. Asked of a
    derived shortcut too, which is the collision that can happen by accident.
    """
    return model_id in _RESERVED or model_id.startswith("claude-")


def family_aliases(models: list[CodexModel]) -> dict[str, str]:
    """Short family shortcuts derived from the chosen ids: ``sol`` -> an id.

    ``sol`` means the newest chosen ``gpt-<ver>-sol``, mirroring the binary's own
    ``opus`` -> ``claude-opus-4-8``: a short handle that resolves to a concrete
    model's id, not an identity of its own. Family and version are read from the
    id itself -- the authoritative slug, never a guessed property or a catalogue
    of ours -- so a model released tomorrow gets its shortcut without patch-cc
    having heard of it.

    Derived, never chosen. There is no way to ask for a shortcut, so a shortcut
    can never disagree with the id it stands for, and nothing downstream has to
    carry a mapping to keep them in step: by the time a request is built the
    binary's own resolver has already rewritten the shortcut to the id.

    Latest wins within a family; an id that is not a clean
    ``gpt-<ver>-<family>`` yields nothing (it still works under its full id), and
    a family that would shadow a built-in or one of the chosen ids is dropped
    rather than guessed.
    """
    best: dict[str, tuple[tuple[int, ...], str]] = {}  # family -> (version, id)
    chosen = {m.id for m in models}
    for model in models:
        match = _FAMILY_RE.match(model.id)
        if not match:
            continue
        family = match.group(2)
        if is_reserved_id(family) or family in chosen:
            continue
        version = tuple(int(part) for part in match.group(1).split("."))
        current = best.get(family)
        if current is None or version > current[0]:
            best[family] = (version, model.id)
    return {family: target for family, (_, target) in best.items()}


# ----------------------------------------------------------------- discovery


def _model_from_entry(entry: dict[str, object]) -> CodexModel | None:
    """One model from either the Codex or the generic model-list shape.

    The Codex backend says ``slug``/``display_name`` (measured; ``title`` was an
    older spelling), the generic OpenAI shape says ``id``/``name`` -- all four are
    read so a renamed key downgrades a label instead of blanking it.

    The window is two keys for the same reason. The Codex backend reports both
    ``max_context_window`` -- the ceiling the model will actually serve -- and
    ``context_window``, the smaller default a client starts at (measured on a
    Codex plan: 872000 against 272000 for gpt-6-astra and the gpt-5.6 family,
    while gpt-5.5 and gpt-5.3-codex-spark report the two equal). Reading the
    default understates the window by two thirds: requests well past it are
    served, and Claude Code -- which is told the number and believes it -- reads
    a long session as full while the model is still answering. The ceiling is
    the honest one, and where only one key exists it is the only one.
    """
    model_id = entry.get("slug") or entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return None
    name = entry.get("display_name") or entry.get("title") or entry.get("name")
    window = entry.get("max_context_window") or entry.get("context_window")
    levels = entry.get("supported_reasoning_levels")
    return CodexModel(
        id=model_id,
        name=name if isinstance(name, str) else "",
        context=window if isinstance(window, int) and window > 0 else 0,
        efforts=tuple(
            effort
            for level in (levels if isinstance(levels, list) else [])
            if isinstance(level, dict)
            and isinstance(effort := level.get("effort"), str)
            and effort
        ),
    )


def _parse(payload: object) -> list[CodexModel]:
    """Read the model array from whichever envelope the backend returned."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("models")
    if not isinstance(raw, list):
        raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    models: list[CodexModel] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict):
            model = _model_from_entry(entry)
            if model is not None and model.id not in seen:
                seen.add(model.id)
                models.append(model)
    return models


def _get(url: str, access_token: str, account_id: str | None) -> object:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
        "User-Agent": BACKEND_UA,
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT) as response:
        return json.loads(response.read())


def discover(
    access_token: str,
    account_id: str | None = None,
    claude_version: str = "",
) -> list[CodexModel]:
    """The models this plan offers, or an empty list if it could not be asked.

    ``claude_version`` is forwarded as the backend's ``client_version`` hint (the
    caller passes the installed version, so nothing about it is assumed here).
    """
    version = urllib.parse.quote(claude_version, safe="") if claude_version else ""
    codex = f"{_BACKEND}/codex/models"
    codex_url = f"{codex}?client_version={version}" if version else codex
    try:
        return _parse(_get(codex_url, access_token, account_id))
    except (OSError, ValueError):
        # URLError is an OSError and JSONDecodeError a ValueError, so these two
        # cover the network, the socket timeout, and the unparseable reply. An
        # empty list is the one answer for all of them, and for a plan that
        # really offers nothing: in every case there is nothing to import.
        return []


def catalogue(claude_version: str = "") -> list[CodexModel]:
    """Every model the signed-in plan offers, or nothing when it cannot be asked.

    Signed out is not an error here, it is one of the ways there is no list -- the
    same answer as an unreachable backend, for the reason :func:`discover` gives
    one answer to all of them. A surface that is *asking* the user to sign in (the
    menu's picker) calls ``discover`` directly instead, so it can say which step
    failed.

    No credentials means no network call at all, so a machine that has never run
    ``codex login`` pays nothing for the surfaces that consult this.
    """
    creds = oauth.load()
    if creds is None:
        return []
    try:
        token, creds = oauth.valid_access(creds)
    except oauth.OAuthError:
        return []
    return discover(token, creds.account_id, claude_version)


def reconcile(
    models: list[CodexModel], claude_version: str = ""
) -> tuple[list[CodexModel], list[str]]:
    """The chosen models with their derived facts refreshed, plus ids the plan drops.

    Called at the moment of baking, by every surface that bakes. Ids are what get
    remembered, so name and window are re-read here rather than carried around:
    that keeps a stale window -- which makes Claude Code auto-compact early --
    from outliving the plan that reported it.

    Best-effort on purpose; ``apply`` must never need the network. A plan that
    cannot be reached also cannot testify that an id is wrong, so nothing is
    reported unknown and every model stands exactly as it came, baking with the
    fallbacks above. When it *can* be reached, an id it does not offer is worth
    knowing now rather than as a mid-session 404 -- and each caller decides what
    that means: strict for ids typed on a command line, lenient for a remembered
    selection whose plan has since changed.

    Models that already carry a name and window (the menu's picker just fetched
    them) survive a silent plan untouched rather than being downgraded to bare
    ids: refreshing may only ever add what we know, never take it away.
    """
    offered = {m.id: m for m in catalogue(claude_version)}
    return (
        [offered.get(m.id, m) for m in models],
        [m.id for m in models if m.id not in offered] if offered else [],
    )
