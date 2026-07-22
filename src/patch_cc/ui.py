"""Shared console helpers, so output styling lives in one place."""

from __future__ import annotations

from rich.console import Console

console = Console()


def heading(text: str) -> None:
    console.print(f"\n[bold]{text}[/bold]")


def ok(text: str) -> None:
    console.print(f"[green]✓[/green] {text}")


def warn(text: str) -> None:
    console.print(f"[yellow]![/yellow] {text}")


def err(text: str) -> None:
    console.print(f"[red]✗[/red] {text}")
