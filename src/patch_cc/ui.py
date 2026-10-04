"""Shared console helpers, so output styling lives in one place."""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from rich.console import Console

from .custom_models import loopback
from .patches.base import Options, Outcome, Patch

if TYPE_CHECKING:
    from .doctor import DryRun, Smoke

console = Console()
#: Warnings and errors go to stderr, so a piped `doctor` or `apply` keeps its
#: report clean and a script can tell the two apart.
_stderr = Console(stderr=True)

#: Glyph and colour per :attr:`Outcome.health`, so the CLI and the menu render
#: the same verdict identically.
MARKS = {"ok": ("✓", "green"), "partial": ("~", "yellow"), "broken": ("✗", "red")}


def findings(outcome: Outcome) -> list[tuple[str, str]]:
    """Every line worth showing under a patch line, as ``(style, text)``.

    One source for *what* gets said; each surface decides only how to draw it,
    so neither can drop the exception that broke a patch or a note the other
    shows -- which is where an early warning goes to die.

    Notes always appear, green runs included: a warning withheld until
    something breaks can only ever arrive late. Absences are the noisy half
    (most patches lack several shapes on any build), so they wait for a verdict
    that is not ``ok``.
    """
    lines = [("red", reason) for reason in outcome.failures()]
    lines += [
        ("yellow", f"sub-step matched but not applied: {name}")
        for name in outcome.missed_steps()
    ]
    if outcome.health != "ok" and (absent := outcome.absent_steps()):
        lines.append(("dim", f"not on this build: {', '.join(absent)}"))
    notes = (*outcome.notes, *(n for s in outcome.steps.values() for n in s.notes))
    return lines + [("dim", note) for note in notes]


def verdicts(result: DryRun, smoke: Smoke | None = None) -> list[tuple[str, str]]:
    """The summary under a dry run's per-patch list, as ``(style, text)`` lines.

    One source for *what a dry run concludes*, the level up from :func:`findings`
    (which words one patch): the parse defect, the broken patches with the anchor
    counts that point at what moved, the drift note for a build that matched but
    could not rewrite, the baked binary's own run when one was made
    (:func:`patch_cc.doctor.smoke` -- the menu passes none, its question is
    matcher health), and the all-clear. Both the CLI and the menu draw these,
    so neither can reach a verdict the other does not -- the exit code is
    :attr:`DryRun.clean` plus the smoke verdict, read once and rendered here
    once.
    """
    lines: list[tuple[str, str]] = []
    _grammar_hint = (
        "not a drifted matcher -- the JS grammar is likely older than the "
        "build; upgrade tree-sitter-javascript"
    )
    _drift = (
        "found a shape they could not rewrite -- drift on a clean bundle, not applied"
    )
    if result.defect is not None:
        lines.append(
            ("yellow", "this build's bundle does not parse; apply would refuse it:")
        )
        lines.append(("red", f"  {result.defect}"))
        lines.append(("dim", _grammar_hint))
    for patch in result.broken:
        lines.append(("yellow", f"{patch.id}: no longer matches -- anchor counts:"))
        for anchor, count in result.anchors.get(patch.id, {}).items():
            lines.append(("red" if count == 0 else "dim", f"  {count:3d}  {anchor}"))
    if result.broken:
        lines.append(
            ("dim", "a 0 is where upstream moved; see docs/PLAYBOOK.md to repair")
        )
    if not result.broken and result.defect is None and result.unhealthy:
        lines.append(("yellow", f"{len(result.unhealthy)} patch(es) {_drift}"))
    for patch, why in result.absent:
        lines.append(("dim", f"{patch.id}: {why}"))
    if smoke is not None and smoke.ok:
        lines.append(("dim", smoke.detail))
    if result.clean:
        matched = "all patches still match this build"
        if result.absent:
            matched += f" ({len(result.absent)} not on it)"
        tail = (
            ", it parses, and the baked binary runs."
            if smoke is not None and smoke.ok
            else ", and it parses."
        )
        lines.append(("green", matched + tail))
    if smoke is not None and not smoke.ok:
        # After the matcher verdict on purpose: matching and running are
        # different truths, and from 2.1.246 (Bun 1.4.1) a written container
        # can be dead on launch with every matcher green.
        lines.append(
            (
                "yellow",
                "the baked binary does not run; the container write is what broke:",
            )
        )
        lines.append(("red", f"  {smoke.detail}"))
    return lines


def applied_value(patch: Patch, outcome: Outcome, options: Options) -> str | None:
    """The value a configurable patch actually wrote, for the report line.

    Branding, the version marker, model overrides, and the chosen custom models
    each carry a chosen value; a plain toggle patch carries none. One source so
    the CLI and the menu report the same thing after an apply.

    Gated on ``health``, not ``applied`` alone: a ``broken`` patch is dropped
    from the binary (the patcher re-runs without it) yet keeps its outcome in
    the report, so echoing its value would assert a feature the bundle does not
    carry -- the very "did it bake?" signal this line exists to keep honest.
    (``health`` is ``broken`` whenever nothing landed too, so no separate
    "applied something" test is needed.)
    """
    if outcome.health == "broken":
        return None
    if patch.id == "branding":
        return options.brand
    if patch.id == "version-marker":
        return options.version_suffix
    if patch.id == "org-label":
        return options.org_label or "hidden"
    if patch.id == "subagent-models" and options.subagent_models:
        return ", ".join(f"{a}={m}" for a, m in options.subagent_models.items())
    if patch.id == "custom-models" and options.custom_models:
        # The models + port that actually landed -- the visible confirmation that
        # custom-models baked (a dropped/no-op patch returned above), and the one
        # place a model dropped for not being offered any more shows as absent.
        ids = ", ".join(m.id for m in options.custom_models)
        return f"{ids}  ·  {options.endpoint}"
    return None


def endpoint_note(url: str) -> tuple[str, str]:
    """A loopback TCP probe is only a listening check, never an API health claim."""
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if not loopback(host):
        return "dim", url
    try:
        with socket.create_connection(
            (host, parsed.port or (443 if parsed.scheme == "https" else 80)),
            timeout=0.25,
        ):
            pass
    except OSError:
        return "yellow", f"{url} · nothing listening — start your proxy"
    return "green", f"{url} · listening"


def heading(text: str) -> None:
    console.print(f"\n[bold]{text}[/bold]")


def ok(text: str) -> None:
    console.print(f"[green]✓[/green] {text}")


def warn(text: str) -> None:
    _stderr.print(f"[yellow]![/yellow] {text}")


def err(text: str) -> None:
    _stderr.print(f"[red]✗[/red] {text}")
