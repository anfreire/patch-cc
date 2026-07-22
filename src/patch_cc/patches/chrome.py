"""UI chrome: spinner tips, the --version marker, and branding."""

from __future__ import annotations

import re

from .base import (
    DEFAULT_SUFFIX,
    GROUP_CHROME,
    IDENT,
    Options,
    Outcome,
    Patch,
    compile_js,
)

# ---------------------------------------------------------- spinner tips

_SPINNER_GUARD = compile_js(
    rf"if\({IDENT}\(\)\.spinnerTipsEnabled===!1\)(?:\{{return;?\}}|return;)"
)
_SPINNER_EXPR = compile_js(rf"{IDENT}\.spinnerTipsEnabled!==!1")


def _disable_spinner_tips(content: str, _options: Options, outcome: Outcome) -> str:
    """Force spinner tips off through both code paths that can enable them.

    Each path counts its candidates from the *setting name*, not from its own
    regex. Counting matches would make "upstream reshaped this path" identical
    to "this build hasn't got it" -- both zero, both silently absent, while the
    other path lands and carries the patch to green with tips still showing.
    Off the setting, a drifted path is `candidates > 0, applied == 0`: a miss.
    """
    guard = outcome.step("guard")
    guard.candidates = content.count("spinnerTipsEnabled===!1")

    def kill_guard(_match: re.Match[str]) -> str:
        guard.applied += 1
        return "if(!0)return;"

    output = _SPINNER_GUARD.sub(kill_guard, content)

    expr = outcome.step("expr")
    expr.candidates = output.count("spinnerTipsEnabled!==!1")

    def kill_expr(_match: re.Match[str]) -> str:
        expr.applied += 1
        return "!1"

    return _SPINNER_EXPR.sub(kill_expr, output)


# ---------------------------------------------------------- version marker

_VERSION_NEEDLE = "}.VERSION} (Claude Code)"


def _js_template_escape(text: str) -> str:
    """Make arbitrary text safe inside a JS template literal."""
    return text.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _version_output(content: str, options: Options, outcome: Outcome) -> str:
    """Append a marker line to plain ``--version`` output.

    The marker text is the user's ``--suffix`` (default ``(patched)``). The
    needle sits inside a template literal, so the suffix is escaped for that
    context.
    """
    marker = "\\n" + _js_template_escape(options.version_suffix or DEFAULT_SUFFIX)
    output = content
    index = output.find(_VERSION_NEEDLE)
    while index != -1:
        outcome.candidates += 1
        marker_at = index + len(_VERSION_NEEDLE)
        if output[marker_at : marker_at + len(marker)] == marker:
            index = output.find(_VERSION_NEEDLE, marker_at + len(marker))
            continue
        output = output[:marker_at] + marker + output[marker_at:]
        outcome.applied += 1
        index = output.find(_VERSION_NEEDLE, marker_at + len(marker))
    return output


# ---------------------------------------------------------- branding


def _branding(content: str, options: Options, outcome: Outcome) -> str:
    """Rename visible ``Claude Code`` branding to the user's chosen name.

    Only the small set of visible startup/help strings are touched, each as its
    own step so partial upstream drift is visible.
    """
    if not options.rebrands:
        outcome.note("brand unchanged; nothing to do")
        return content

    brand = options.brand
    esc = brand.replace("\\", "\\\\").replace('"', '\\"')
    output = content

    def swap(step_name: str, pattern: re.Pattern[str], replace) -> None:
        nonlocal output
        step = outcome.step(step_name)

        def rewrite(match: re.Match[str]) -> str:
            step.candidates += 1
            result = replace(match)
            if result != match.group(0):
                step.applied += 1
            return result

        output = pattern.sub(rewrite, output)

    swap(
        "bold-text",
        compile_js(
            rf'({IDENT})\.createElement\(({IDENT}),\{{bold:!0\}},"Claude Code"\)'
        ),
        lambda m: f'{m.group(1)}.createElement({m.group(2)},{{bold:!0}},"{esc}")',
    )
    swap(
        "bold-jsx",
        compile_js(
            rf'({IDENT})\.(jsx|jsxs)\(({IDENT}),\{{bold:!0,children:"Claude Code"\}}\)'
        ),
        lambda m: (
            f'{m.group(1)}.{m.group(2)}({m.group(3)},{{bold:!0,children:"{esc}"}})'
        ),
    )
    swap(
        "help-title",
        compile_js(
            r"title:(`Claude Code v\$\{[\s\S]*?\.VERSION\}`),"
            r'color:"professionalBlue",defaultTab:"general"'
        ),
        lambda m: (
            f'title:{m.group(1)}.replace("Claude Code","{esc}"),'
            f'color:"professionalBlue",defaultTab:"general"'
        ),
    )
    swap(
        "welcome-for",
        compile_js(r'"Welcome to Claude Code for "'),
        lambda _m: f'"Welcome to {esc} for "',
    )
    swap(
        "welcome",
        compile_js(r'"Welcome to Claude Code"'),
        lambda _m: f'"Welcome to {esc}"',
    )
    swap(
        "children-array",
        compile_js(r'(color:"claude",bold:!0,children:\[)"Claude Code"(," "\])'),
        lambda m: f'{m.group(1)}"{esc}"{m.group(2)}',
    )
    swap(
        "styled-title",
        compile_js(rf'({IDENT})\("claude",({IDENT})\)\("Claude Code"\)'),
        lambda m: f'{m.group(1)}("claude",{m.group(2)})("{esc}")',
    )
    swap(
        "styled-title-padded",
        compile_js(rf'({IDENT})\("claude",({IDENT})\)\(" Claude Code "\)'),
        lambda m: f'{m.group(1)}("claude",{m.group(2)})(" {esc} ")',
    )

    return output


PATCHES = [
    Patch(
        id="spinner-tips",
        title="Disable spinner tips",
        summary="Stop the loading spinner from showing rotating tips.",
        group=GROUP_CHROME,
        fn=_disable_spinner_tips,
        default=False,
        anchors=("spinnerTipsEnabled",),
    ),
    Patch(
        id="version-marker",
        title="Mark --version as patched",
        summary="Append a marker line to `claude --version` (custom text via --suffix).",
        group=GROUP_CHROME,
        fn=_version_output,
        anchors=("}.VERSION} (Claude Code)",),
    ),
    Patch(
        id="branding",
        title="Custom startup name",
        summary="Rename the startup/help branding (default: your username's Code).",
        group=GROUP_CHROME,
        fn=_branding,
        anchors=('"Welcome to Claude Code"', '{bold:!0},"Claude Code"'),
    ),
]
