# patch-cc

[![CI](https://github.com/anfreire/patch-cc/actions/workflows/ci.yml/badge.svg)](https://github.com/anfreire/patch-cc/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/patch-cc)](https://pypi.org/project/patch-cc/)

An interactive patcher for the **Claude Code native binary**. Pick the tweaks
you want — inline and live thinking, detailed tool calls, subagent model
overrides, your own startup name, **custom models through your own
Anthropic-compatible endpoint** — and apply them to your installed `claude`.
Fully reversible: a pristine backup is kept, `patch-cc restore` puts it back.
No Node, no Bun.

```bash
uvx --no-cache patch-cc        # fullscreen menu, no install needed
```

![patch-cc interactive patcher](https://raw.githubusercontent.com/anfreire/patch-cc/main/docs/demo.gif)

## Requirements

- **Linux or macOS**
- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — how patch-cc is run and installed
  below. Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  Not using uv? `pipx install patch-cc` (or `pip install patch-cc`) works too;
  it is an ordinary PyPI package.
- **macOS only:** the Xcode command line tools, for `codesign` — a patched
  binary has to be re-signed or macOS refuses to run it.

The menu is a single centered panel: move with `↑ ↓`, toggle with `space`,
press `s` to apply. Patches that carry a setting — subagent models, custom models,
the startup name, the `--version` marker, the org/email label — open a centered
modal on `enter`, and the row then shows what you chose. Everything choosable
is a picker: the agent names and model aliases are **discovered from your
binary itself** (and custom models from your endpoint, with manual entry available), so the menu can never
offer something your build would reject. Typing exists only for the genuinely
free-text values.

The menu remembers your selection, including the settings of patches you
switch off, and its header says when the binary differs from it.

Prefer it always available on your PATH? Install it:

```bash
uv tool install patch-cc
patch-cc                       # then just run it
```

## What it can do

| Group | Patch | |
|---|---|---|
| Output & display | Detailed tool calls | Show full read/search calls, not collapsed summaries |
| | Colour new files as diffs | Created files render with `+` lines and green |
| | Fix blank thinking blocks | Opt out of the server-side experiment that can empty every thinking block |
| | Always show thinking | Thinking blocks stay inline — no `ctrl+o` |
| | Stream thinking live | See reasoning as it is generated, inline and in order |
| | Show subagent prompts | Prompt blocks visible during normal use |
| Models & effort | Persist max effort | `/effort max` saves as your default for new sessions, like the other levels |
| | Custom models | Register external models and route them to an Anthropic-compatible endpoint — see [Custom models](#custom-models) |
| | Override subagent models | Pick the model per built-in agent (discovered from your binary) |
| Chrome & branding | Disable spinner tips | No rotating tips on the spinner |
| | Mark `--version` | Appends `(patched)` — or any marker you choose |
| | Custom startup name | Defaults to `<your username>'s Code` |
| | Startup org/email label | Replace the org/email on the welcome screen — or hide it (demo mode keeps the stock line). Upstream stopped drawing the segment in 2.1.246, so newer builds do not offer it |

## Usage

The menu is the one place choices are made; two commands act on them (shown
with `uvx --no-cache`; drop it if you installed the tool):

```bash
uvx --no-cache patch-cc apply     # bake the saved selection — after a Claude update, this is all you need
uvx --no-cache patch-cc restore   # put the original back
```

`apply` takes no flags: it bakes exactly what the menu would open on — your
saved selection, else what the installed binary already records, else the
default set — and says what this build cannot honour before it writes.
Agents and models are checked against what your installed binary actually
ships, and custom model ids against Claude's own names. The saved selection
is a readable file (`~/.cache/patch-cc/selection.json`, under
`$XDG_CACHE_HOME` when set) if you ever want to script it.

## Custom models

Register models served by your own **Anthropic Messages endpoint** beside
Claude's: in `/model`, in the status line, and as subagent targets. The endpoint
can be a proxy (CLIProxyAPI, OmniRoute, 9router, LiteLLM, …), a provider's
Anthropic-compatible API, or a local server such as Ollama; patch-cc runs no
server and vouches for none of them. If you have no preference,
[EasyCLIProxyAPI](https://github.com/router-for-me/EasyCLIProxyAPI) is the one patch-cc
is tested against. Only the chosen models' requests are diverted. Claude models
keep their endpoint, your login and every feature.

In the menu, open **Custom models** (`?` there opens this section) and set
the **Endpoint**: the base URL before `/v1/messages`, such as
`http://127.0.0.1:8317` or `https://api.z.ai/api/anthropic`. Add a **Key** if
the endpoint needs one, then open **Models**: `tab` lists what the endpoint
offers, `space` picks, `a` adds an id by hand, and `enter` edits a model's
name, `/model` alias, context window and effort levels. Names, windows and
levels come from the endpoint where it reports them; a model with a default
and a larger window offers both, and the window and effort pickers take your
own value under `c` (`256k`, `272000`, `1m`; a comma-separated ladder). Without
a window, Claude's 200K default stands, and nothing checks a number you type.
Discovery only suggests: `apply` never contacts the endpoint and bakes exactly
what was chosen, so it replays unchanged after a Claude update.

**The key.** The patched binary sends `PATCH_CC_API_KEY` when it is set, and
otherwise the key the menu saves to `~/.local/share/patch-cc/api-key` (mode 600,
under `$XDG_DATA_HOME` when set; the path is resolved when you apply and baked
as-is). It goes out as both `Authorization: Bearer` and `x-api-key`, read per
request, so a new key needs no re-apply. Your Claude login is removed from those
requests. No key means no credential header, which is what keyless local
servers expect. The key never enters the binary or the saved selection.

**What the endpoint must speak** is Anthropic's
[gateway protocol](https://code.claude.com/docs/en/llm-gateway-protocol). Routed
turns always carry the full history and never mid-conversation system messages
or context edits; token counting may 404. Three things endpoints get wrong:

- The final `message_delta.usage` must be the whole object, with cache tokens
  subtracted and `input_tokens` at least 1.
- `stop_reason: "refusal"` makes Claude Code re-run the turn on a Claude
  fallback model at Anthropic, carrying the routed conversation.
- CLIProxyAPI and LiteLLM listen on all interfaces by default. Bind them to
  `127.0.0.1` (`server.host`, `--host`).

HTTP is accepted on loopback only; remote endpoints need HTTPS. Effort is a
request setting the endpoint translates; the menu offers the ladder cut at
each top the binary can express, or **off**. A model that reports no levels
starts off — nothing is offered that was not declared, since Claude Code
would otherwise offer every level to a model it does not know. Levels a model
excludes are switched off from Claude 2.1.267 on, through
`CLAUDE_CODE_MODEL_CAPABILITIES`, and your own entries in that variable win.
Managed `availableModels` allowlists still apply. Bedrock, Vertex and Foundry
modes are out of scope.

## After a Claude update

Claude auto-updates roughly daily and replaces the binary, which reverts the
patch. Run `patch-cc apply` — it bakes your saved selection and says what the
new build cannot honour — or open `patch-cc`, whose header reads **pending
apply** until you do. The startup name and the `--version` marker are visible
tells too.

## Why native-only, and what the write does

Claude Code ships only as a Bun single-file executable; the npm package is a
wrapper that downloads it. patch-cc edits the JavaScript modules embedded in the
binary's `.bun` section — since 2.1.242 the app is code-split across more than a
thousand of them, and patch-cc treats every one as a single surface. The write never moves a
byte of the original: each edited module's source is appended and its stale
precompiled bytecode is unlinked (editing a module's source invalidates its
bytecode anyway), so a patched binary is a few percent larger than the original,
and everything else the binary carries — Bun's own records, however its format
grows — is exactly where it was. That is what keeps the container layer out of
the way when Bun changes its format under Claude.
[docs/INTERNALS.md](docs/INTERNALS.md#the-rule-never-move-a-pristine-byte) has
the reasoning; the apply report has your numbers.

See [docs/INTERNALS.md](docs/INTERNALS.md) for the container format and
[docs/PLAYBOOK.md](docs/PLAYBOOK.md) for repairing a patch after an update.
Changing anything here starts at [docs/CONDUCT.md](docs/CONDUCT.md) — how this
is built, and what a patch has to prove before it ships.

## Credits

The patch set is a Python port of
[a-connoisseur/patch-claude-code](https://github.com/a-connoisseur/patch-claude-code),
with the subagent-model override idea from
[aleks-apostle/claude-code-patches](https://github.com/aleks-apostle/claude-code-patches).
Registering external models inside the bundle follows
[clodex](https://github.com/gxjansen/clodex); the routing here is done in the
bundle rather than with clodex's TLS interception.

## License

MIT

---

**More agent tooling** — [summon-cc](https://github.com/anfreire/summon-cc): give your agent a crew of Claude Code workers · [cc-oc](https://github.com/anfreire/cc-oc): drive opencode from inside Claude Code · [omoctl](https://github.com/anfreire/omoctl): manage oh-my-openagent profiles · [wiki-spaces](https://github.com/anfreire/wiki-spaces): a wiki your AI agent keeps
