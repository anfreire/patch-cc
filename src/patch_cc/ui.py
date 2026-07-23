"""Shared console helpers, so output styling lives in one place."""

from __future__ import annotations

from rich.console import Console

from .patches.base import Options, Outcome, Patch

console = Console()

#: Glyph and colour per :attr:`Outcome.health`, so the CLI and the menu render
#: the same verdict identically.
MARKS = {"ok": ("✓", "green"), "partial": ("~", "yellow"), "broken": ("✗", "red")}


def findings(outcome: Outcome) -> list[tuple[str, str]]:
    """Every line worth showing under a patch line, as ``(style, text)``.

    One source for *what* gets said; each surface decides only how to draw it.
    The CLI and the menu built this separately and had already drifted -- the
    menu showed neither the exception that broke a patch nor any note, which is
    where an early warning goes to die.

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


def applied_value(patch: Patch, options: Options) -> str | None:
    """The value a configurable patch actually wrote, for the report line.

    Branding, the version marker, and model overrides each carry a chosen value;
    a plain toggle patch carries none. One source so the CLI and the menu report
    the same thing after an apply.
    """
    if patch.id == "branding":
        return options.brand
    if patch.id == "version-marker":
        return options.version_suffix
    if patch.id == "subagent-models" and options.subagent_models:
        return ", ".join(f"{a}={m}" for a, m in options.subagent_models.items())
    return None


def heading(text: str) -> None:
    console.print(f"\n[bold]{text}[/bold]")


def ok(text: str) -> None:
    console.print(f"[green]✓[/green] {text}")


def warn(text: str) -> None:
    console.print(f"[yellow]![/yellow] {text}")


def err(text: str) -> None:
    console.print(f"[red]✗[/red] {text}")
