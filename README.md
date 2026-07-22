# patch-cc

[![CI](https://github.com/anfreire/patch-cc/actions/workflows/ci.yml/badge.svg)](https://github.com/anfreire/patch-cc/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/patch-cc)](https://pypi.org/project/patch-cc/)

An interactive patcher for the **Claude Code native binary**. Pick the tweaks
you want — inline and live thinking, detailed tool calls, subagent model
overrides, your own startup name — and apply them to your installed `claude`
in one keystroke. Pure Python; no Node, no Bun.

## Requirements

- **Linux or macOS**
- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** — how patch-cc is run and installed
  below. Install it with `curl -LsSf https://astral.sh/uv/install.sh | sh`.
  Not using uv? `pipx install patch-cc` (or `pip install patch-cc`) works too;
  it is an ordinary PyPI package.
- **macOS only:** the Xcode command line tools, for `codesign` — a patched
  binary has to be re-signed or macOS refuses to run it.

```bash
uvx patch-cc                   # fullscreen menu, no install needed
```

The menu is a single centered panel: move with `↑ ↓`, toggle with `space`,
press `s` to save. Patches that carry a setting — subagent models, the startup
name, the `--version` marker — open a centered modal on `enter`, and the row
then shows what you chose. Everything choosable is a picker: the agent names
and model aliases are **discovered from your binary itself**, so the menu can
never offer something your build would reject. Typing exists only for the two
genuinely free-text values.

A patched binary records what was applied inside itself, so the menu always
comes up showing the real current state, and `patch-cc status` answers
exactly.

Prefer it always available on your PATH? Install it:

```bash
uv tool install patch-cc
patch-cc                       # then just run it
```

## What it can do

| Group | Patch | |
|---|---|---|
| Output & diffs | Detailed tool calls | Show full read/search calls, not collapsed summaries |
| | Colour new files as diffs | Created files render with `+` lines and green |
| Thinking | Always show thinking | Thinking blocks stay inline — no `ctrl+o` |
| Live thinking | Stream thinking live | See reasoning as it is generated, inline and in order |
| Subagents | Show subagent prompts | Prompt blocks visible during normal use |
| | Override subagent models | Pick the model per built-in agent (discovered from your binary) |
| Chrome | Disable spinner tips | No rotating tips on the spinner |
| | Custom startup name | Defaults to `<your username>'s Code` |
| | Mark `--version` | Appends `(patched)` — or any marker you choose |

## Usage

Everything the menu does is also a non-interactive subcommand (shown with
`uvx`; drop it if you installed the tool):

```bash
uvx patch-cc apply                            # the default patch set
uvx patch-cc apply tool-calls live-thinking   # just these
uvx patch-cc apply --brand                    # + branding as <username>'s Code
uvx patch-cc apply --brand "Ada's Code"       # + branding, explicit name
uvx patch-cc apply --model Explore=haiku --model general-purpose=opus
uvx patch-cc apply --suffix "(mine)"          # custom --version marker
uvx patch-cc status                           # exactly what is applied
uvx patch-cc doctor                           # do all patches match this build?
uvx patch-cc list                             # patches + your binary's agents/models
uvx patch-cc restore                          # put the original back
```

`--model` and `--brand` imply their patches; agents and models are validated
against what your installed binary actually ships.

## After a Claude update

Claude auto-updates roughly daily and replaces the binary, which reverts the
patch. Re-run `patch-cc` — the menu remembers your last selection — or re-apply
your set explicitly:

```bash
uvx patch-cc apply --brand --model Explore=haiku
```

`uvx patch-cc status` tells you whether the current binary is patched, and the
startup name / `--version` marker are visible tells too.

## Why native-only, and why it stays small

Claude Code now ships only as a Bun single-file executable; the npm package is a
wrapper that downloads it. patch-cc edits the JavaScript bundle embedded in the
binary's `.bun` section in place. It also drops the module's 154 MB of stale
precompiled bytecode — editing the source invalidates it anyway — so a patched
binary is *smaller* than the original (≈113 MB vs 267 MB), not larger.

See [docs/INTERNALS.md](docs/INTERNALS.md) for the container format and
[docs/PLAYBOOK.md](docs/PLAYBOOK.md) for repairing a patch after an update.

## Credits

The patch set is a Python port of
[a-connoisseur/patch-claude-code](https://github.com/a-connoisseur/patch-claude-code),
with the subagent-model override idea from
[aleks-apostle/claude-code-patches](https://github.com/aleks-apostle/claude-code-patches).

## License

MIT
