"""Custom-model configuration, the endpoint key, and optional model discovery.

Discovery suggests ids for the menu; applying never consults a server.
The key lives in patch-cc's data directory, never in a baked setting.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

KEY_ENV = "PATCH_CC_API_KEY"
EFFORT_LADDER = ("low", "medium", "high", "xhigh", "max")
#: The two words the menu's model pickers mean something else by. Claude's own
#: names are not listed here: the binary in hand says what those are
#: (`patches.custom_models.claimed_model_names`), and a list would fall behind it.
_MENU_WORDS = {"inherit", "keep"}
_ID = re.compile(r"[a-z0-9][a-z0-9._:/-]*")


class ConfigurationError(ValueError):
    """Invalid user-supplied or persisted model configuration."""


@dataclass(frozen=True, slots=True)
class CustomModel:
    id: str
    name: str = ""
    context: int = 0
    #: The levels the model is declared to run; empty switches the control off.
    #: There is no "unknown": on the API path an undeclared model is offered
    #: every level, which is the opposite of not knowing, so a model that
    #: reports nothing starts off and is turned on by declaring its ladder.
    efforts: tuple[str, ...] = ()
    alias: str = ""
    #: Reported window choices; only context selects the value baked into Claude.
    context_options: tuple[int, ...] = ()

    @property
    def label(self) -> str:
        return self.name or self.id


def model_id(value: str) -> str:
    if not _ID.fullmatch(value) or any(not part for part in value.split("/")):
        raise ConfigurationError(
            "model ids use lowercase letters, digits and . _ - / :"
        )
    if value.startswith("claude-"):
        raise ConfigurationError(f"{value!r} is a Claude model id")
    if value in _MENU_WORDS:
        raise ConfigurationError(f"{value!r} is a word the menu uses")
    return value


def context_tokens(value: str) -> int:
    match = re.fullmatch(r"(\d+)([km]?)", value.strip().lower())
    if match is None:
        raise ConfigurationError(
            "context must be tokens, such as 272000, 272k or 1m (0 = unknown)"
        )
    return int(match[1]) * {"": 1, "k": 1000, "m": 1_000_000}[match[2]]


def effort_levels(value: str) -> tuple[str, ...]:
    value = value.strip().lower()
    if value in ("", "off"):
        return ()
    levels = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if any(
        level not in (*EFFORT_LADDER, "none", "minimal", "ultra") for level in levels
    ):
        raise ConfigurationError("efforts must be comma-separated levels, or off")
    return levels


def parse_model(spec: str) -> CustomModel:
    identity, sep, window = spec.partition("=")
    return CustomModel(model_id(identity), context=context_tokens(window) if sep else 0)


def model_alias(value: str) -> str:
    return model_id(value.strip()) if value.strip() else ""


def models_from(value: object) -> list[CustomModel]:
    """Validate saved models through the same boundary as typed configuration."""
    if not isinstance(value, list):
        raise ConfigurationError("custom models must be a list")
    found: dict[str, CustomModel] = {}
    for item in value:
        if isinstance(item, str):
            model = parse_model(item)
        elif isinstance(item, dict):
            identity = item.get("id")
            window = item.get("context", 0)
            name = item.get("name", "")
            efforts = item.get("efforts") or []
            alias = item.get("alias", "")
            windows = item.get("context_options", [])
            if not isinstance(identity, str) or not isinstance(name, str):
                raise ConfigurationError("model id and name must be strings")
            if type(window) is not int or window < 0:
                raise ConfigurationError("model context must be a non-negative integer")
            if not isinstance(alias, str):
                raise ConfigurationError("model alias must be a string")
            if not isinstance(windows, (list, tuple)) or any(
                type(value) is not int or value <= 0 for value in windows
            ):
                raise ConfigurationError(
                    "reported context options must be positive integers"
                )
            if not isinstance(efforts, (list, tuple)) or any(
                not isinstance(level, str) for level in efforts
            ):
                raise ConfigurationError("model efforts must be a list")
            model = CustomModel(
                model_id(identity),
                name,
                window,
                effort_levels(",".join(efforts)),
                alias=model_alias(alias),
                context_options=tuple(dict.fromkeys(windows)),
            )
        else:
            raise ConfigurationError("each custom model must be an id or model object")
        found[model.id] = model
    models = list(found.values())
    names = {model.id for model in models}
    for model in models:
        if model.alias:
            if model.alias in names:
                raise ConfigurationError(
                    f"alias {model.alias!r} collides with a model id or another alias"
                )
            names.add(model.alias)
    return models


def model_values(models: list[CustomModel]) -> list[dict]:
    """The saved shape: tuples as lists, and the optional fields only when set."""
    return [
        {
            key: list(value) if isinstance(value, tuple) else value
            for key, value in asdict(model).items()
            if key not in ("alias", "context_options") or value
        }
        for model in models
    ]


def aliases(models: list[CustomModel]) -> dict[str, str]:
    return {model.alias: model.id for model in models if model.alias}


def model_names(models: list[CustomModel]) -> list[str]:
    return [*[model.id for model in models], *aliases(models)]


def endpoint(value: str) -> str:
    """An Anthropic API base; never a credential-bearing URL."""
    if not value or any(c.isspace() or ord(c) < 32 for c in value):
        raise ConfigurationError(
            "endpoint must be an http(s) base URL without whitespace"
        )
    try:
        url = urllib.parse.urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise ConfigurationError("endpoint has an invalid host or port") from exc
    if url.scheme not in ("http", "https") or not url.hostname or port == 0:
        raise ConfigurationError(
            "endpoint must be an http(s) base URL with a valid host and port"
        )
    if (
        url.username is not None
        or url.password is not None
        or "?" in value
        or "#" in value
    ):
        raise ConfigurationError(
            "endpoint must not contain credentials, a query or a fragment"
        )
    if url.scheme == "http" and not loopback(url.hostname):
        raise ConfigurationError(
            "remote endpoints require HTTPS; use a loopback tunnel for HTTP"
        )
    return urllib.parse.urlunsplit(
        (url.scheme, url.netloc, url.path.rstrip("/"), "", "")
    )


def loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def key_path() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "patch-cc" / "api-key"


def key_value(value: str) -> str:
    value = value.strip()
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise ConfigurationError("endpoint key must contain printable ASCII characters")
    return value


def read_key() -> str:
    """The key the patched binary sends: the environment first, then the saved file."""
    value = os.environ.get(KEY_ENV)
    if value is None:
        try:
            value = key_path().read_text("utf8")
        except FileNotFoundError:
            value = ""
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError(
                f"cannot read the endpoint key at {key_path()}"
            ) from exc
    return key_value(value)


def save_key(value: str) -> None:
    """Store a private key file, or remove it for keyless access."""
    path = key_path()
    value = key_value(value)
    if not value:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".api-key-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(value + "\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class DiscoveryError(ValueError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(url: str, key: str) -> object:
    from . import __version__

    headers = {"Accept": "application/json", "User-Agent": f"patch-cc/{__version__}"}
    if key:
        headers.update({"Authorization": f"Bearer {key}", "x-api-key": key})
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect).open(
            request, timeout=3
        ) as response:
            raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise DiscoveryError("endpoint catalogue is too large")
            return json.loads(raw)
    except DiscoveryError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DiscoveryError(
                "endpoint rejected the key; check its client API key"
            ) from exc
        if 300 <= exc.code < 400:
            raise DiscoveryError(
                "endpoint redirected discovery; enter its final base URL"
            ) from exc
        raise DiscoveryError(
            f"model discovery returned HTTP {exc.code}; models can be entered manually"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise DiscoveryError(
            "endpoint is unreachable; check the URL and start your proxy"
        ) from exc
    except ValueError as exc:
        raise DiscoveryError("endpoint did not return a JSON model catalogue") from exc


def _positive(entry: dict, *names: str) -> int:
    return next(
        (
            entry[name]
            for name in names
            if type(entry.get(name)) is int and entry[name] > 0
        ),
        0,
    )


def _catalogue(payload: object) -> list[CustomModel]:
    if not isinstance(payload, dict):
        return []
    entries = payload.get("models", payload.get("data", []))
    if not isinstance(entries, list):
        return []
    found: dict[str, CustomModel] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility") == "hide":
            continue
        identity = entry.get("slug", entry.get("id"))
        if not isinstance(identity, str):
            continue
        try:
            identity = model_id(identity)
        except ValueError:
            continue
        name = entry.get("display_name", entry.get("displayName", ""))
        window = _positive(
            entry,
            "context_window",
            "context_length",
            "max_input_tokens",
            "inputTokenLimit",
        )
        windows = tuple(
            dict.fromkeys(
                value
                for value in (window, _positive(entry, "max_context_window"))
                if value
            )
        )
        levels = entry.get("supported_reasoning_levels")
        if levels is None and isinstance(entry.get("thinking"), dict):
            levels = entry["thinking"].get("levels")
        efforts: tuple[str, ...] = ()
        if isinstance(levels, list) and levels:
            try:
                efforts = effort_levels(
                    ",".join(
                        level.get("effort", "")
                        if isinstance(level, dict)
                        else str(level)
                        for level in levels
                    )
                )
            except ValueError:
                pass
        found[identity] = CustomModel(
            identity,
            name if isinstance(name, str) else "",
            window,
            efforts,
            context_options=windows,
        )
    return list(found.values())


def discover(url: str, key: str) -> list[CustomModel]:
    """Read the endpoint's ids, enriched by its optional client catalogue.

    No Anthropic headers: some endpoints cloak non-Claude ids for that client.
    Reported names, windows and efforts are suggestions the user can edit.
    """
    url = endpoint(url)
    key = key_value(key)
    ordinary = _get(url + "/v1/models?limit=1000", key)
    models = _catalogue(ordinary)
    if isinstance(ordinary, dict) and isinstance(ordinary.get("data"), list):
        # The ids above are the whole feature. CLIProxyAPI keeps names, windows
        # and effort levels in a client catalogue (`client_version=cpa`:
        # `models[]` with `slug`, `visibility`, `max_context_window`,
        # `supported_reasoning_levels`), joined on only when it answers; any
        # other endpoint stops at the ids.
        try:
            rich = _get(url + "/v1/models?client_version=cpa", key)
        except DiscoveryError:
            rich = None
        if isinstance(rich, dict) and isinstance(rich.get("models"), list):
            available = {model.id for model in models}
            hidden = {
                item.get("slug", item.get("id"))
                for item in rich["models"]
                if isinstance(item, dict) and item.get("visibility") == "hide"
            }
            by_id = {
                model.id: model for model in _catalogue(rich) if model.id in available
            }
            models = [
                by_id.get(model.id, model) for model in models if model.id not in hidden
            ]
    if not models:
        raise DiscoveryError(
            "endpoint offered no usable custom models; enter model ids manually"
        )
    return sorted(models, key=lambda model: (model.label.casefold(), model.id))
