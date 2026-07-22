"""Command-line entry point.

Bare ``patch-cc`` opens the interactive menu. Every action is also a
subcommand so nothing needs the TUI:

    patch-cc apply [PATCH ...] [--brand [NAME]] [--model AGENT=MODEL]
                   [--suffix TEXT]
    patch-cc status
    patch-cc doctor
    patch-cc list
    patch-cc restore
    patch-cc extract PATH        # dump the JS bundle (debugging)

Patch ids are positional; nothing selected means the default set. The two
configurable patches ride on their option: ``--brand`` selects branding,
``--model`` selects subagent-models. Agents and models are validated against
what the installed binary itself offers.
"""

from __future__ import annotations

import argparse
import sys

from . import locate, patcher
from .bun import BunError
from .patches import Options, by_group, default_ids, derived_brand, ids
from .patches.agents import INHERIT, discover_agents, discover_models
from .ui import console, err, heading, ok, warn

#: ``--brand`` with no value: derive the name from the system username.
_DERIVE = ""


def _parse_models(specs: list[str], source: str) -> dict[str, str]:
    """Validate ``AGENT=MODEL`` pairs against what this bundle offers."""
    agents = {a.name: a for a in discover_agents(source)}
    models = [INHERIT, *discover_models(source)]
    overrides: dict[str, str] = {}
    for spec in specs:
        agent, sep, model = spec.partition("=")
        if not sep or agent not in agents or model not in models:
            err(f"--model expects AGENT=MODEL, got {spec!r}")
            console.print(
                f"  [dim]agents in this binary: {', '.join(sorted(agents))}[/dim]"
            )
            console.print(f"  [dim]models in this binary: {', '.join(models)}[/dim]")
            raise SystemExit(2)
        overrides[agent] = model
    return overrides


def _requested(args, source: str) -> tuple[list[str], Options]:
    """Build the patch set and options purely from CLI args -- no saved state.

    Non-interactive patching is deliberately stateless: what you pass is
    exactly what you get, the default set when you pass nothing.
    """
    selected = list(args.patches) if args.patches else default_ids()
    unknown = [p for p in selected if p not in ids()]
    if unknown:
        err(f"unknown patch id(s): {', '.join(unknown)}")
        console.print(f"  [dim]valid ids: {', '.join(ids())}[/dim]")
        raise SystemExit(2)

    options = Options()
    if args.brand is not None:
        options.brand = args.brand.strip() or derived_brand()
        if "branding" not in selected:
            selected.append("branding")
    elif "branding" in selected:
        options.brand = derived_brand()

    if args.suffix:
        options.version_suffix = args.suffix
        if "version-marker" not in selected:
            selected.append("version-marker")

    if args.model:
        options.subagent_models = _parse_models(args.model, source)
        if "subagent-models" not in selected:
            selected.append("subagent-models")
    elif "subagent-models" in selected:
        err("subagent-models needs at least one --model AGENT=MODEL")
        agents = discover_agents(source)
        if agents:
            console.print(
                f"  [dim]agents in this binary: "
                f"{', '.join(sorted(a.name for a in agents))}[/dim]"
            )
        raise SystemExit(2)

    return selected, options


def _print_report(report: patcher.PatchReport) -> None:
    heading("Patch results")
    for patch, outcome in report.results:
        missed = outcome.missed_steps()
        if outcome.landed and not missed:
            mark = "[green]✓[/green]"
        elif outcome.landed:
            mark = "[yellow]~[/yellow]"
        else:
            mark = "[red]✗[/red]"
        detail = f"  applied {outcome.applied}" if outcome.applied else ""
        console.print(f"  {mark} {patch.title:28s}{detail}")
        for name in missed:
            console.print(
                f"      [yellow]· sub-step matched but not applied:[/yellow] {name}"
            )
        for note in outcome.notes:
            console.print(f"      [dim]· {note}[/dim]")

    if report.output is None:
        console.print()
        err("No patch changed anything; the binary was left untouched.")
        console.print("  [dim]Run `patch-cc doctor` for anchor details.[/dim]")
        return

    saved = report.original_size - report.patched_size
    size_note = (
        f"{abs(saved) / 1e6:.0f} MB smaller than original"
        if saved > 0
        else f"{abs(saved) / 1e6:.0f} MB larger than original"
        if saved < 0
        else "same size as original"
    )
    console.print()
    ok(f"Wrote {report.output}  ({report.patched_size / 1e6:.0f} MB, {size_note})")
    if report.backup:
        console.print(f"  [dim]backup: {report.backup}[/dim]")
    if report.regressions:
        warn(
            f"{len(report.regressions)} patch(es) matched nothing: "
            + ", ".join(p.id for p in report.regressions)
        )
        console.print("  [dim]Run `patch-cc doctor` for anchor details.[/dim]")


def cmd_apply(args) -> int:
    install = locate.find_or_raise()
    bundle = patcher.read_pristine(install)
    selected, options = _requested(args, bundle.source)

    heading(f"Patching Claude {install.version or '?'} ({install.binary.name})")
    try:
        report = patcher.patch_installation(install, selected, options, bundle=bundle)
    except patcher.AlreadyPatchedError as exc:
        warn(str(exc))
        return 1
    except BunError as exc:
        err(str(exc))
        return 1

    _print_report(report)
    if report.output is not None:
        console.print("\n[dim]Restart Claude Code for changes to take effect.[/dim]")
    return 0 if report.output is not None else 1


def cmd_status(args) -> int:
    from . import doctor
    from .bun import container

    install = locate.find_or_raise()
    bundle = container.read(str(install.binary))
    st = doctor.status(bundle)

    heading(f"Claude {install.version or '?'}  ({install.binary})")
    state = "[green]patched[/green]" if st.patched else "[yellow]not patched[/yellow]"
    console.print(f"  state:     {state}")
    if st.manifest:
        tool = st.manifest.get("tool", "?")
        console.print(f"  by:        patch-cc {tool}")
        console.print(f"  patches:   {', '.join(st.patch_ids) or '-'}")
        if "brand" in st.manifest:
            console.print(f"  brand:     {st.manifest['brand']}")
        if "suffix" in st.manifest:
            console.print(f"  suffix:    {st.manifest['suffix']}")
        for agent, model in (st.manifest.get("models") or {}).items():
            console.print(f"  model:     {agent} = {model}")
    elif st.patched:
        console.print(
            "  [dim]patched by an older patch-cc (no manifest); "
            "re-apply to record one[/dim]"
        )
    console.print(
        f"  bytecode:  "
        f"{'stripped' if st.bytecode_stripped else f'{st.bytecode_size / 1e6:.0f} MB present'}"
    )
    backup = patcher.backup_path_for(install)
    if backup.exists():
        console.print(f"  backup:    {backup}")
    if install.is_symlinked:
        console.print(f"  [dim]launcher {install.launcher} -> {install.binary}[/dim]")
    return 0


def cmd_doctor(args) -> int:
    from . import doctor
    from .bun import container

    install = locate.find_or_raise()
    installed = container.read(str(install.binary))

    # Matcher health must be tested on a clean bundle. If the installed binary
    # is already patched, our edits have removed the anchors, so fall back to
    # the pristine backup.
    test_bundle = installed
    source_note = ""
    if patcher.is_patched(installed.source):
        clean = patcher.clean_source_path(install)
        if clean is None:
            warn("Installed binary is already patched and no clean backup exists.")
            console.print(
                "  [dim]Matcher health can't be checked against a patched binary. "
                "Run `patch-cc restore`, or test a freshly downloaded binary.[/dim]"
            )
            return 1
        test_bundle = container.read(str(clean))
        source_note = (
            "  [dim](installed binary is patched; testing against backup)[/dim]"
        )

    result = doctor.dryrun(test_bundle)

    heading(f"Patch health against Claude {install.version or '?'}{source_note}")
    for check in result.checks:
        outcome = check.outcome
        missed = outcome.missed_steps()
        if outcome.landed and not missed:
            mark, colour = "✓", "green"
        elif outcome.landed:
            mark, colour = "~", "yellow"
        else:
            mark, colour = "✗", "red"
        console.print(
            f"  [{colour}]{mark}[/{colour}] {check.patch.id:20s} "
            f"cand={outcome.candidates} applied={outcome.applied}"
        )
        for name in missed:
            sub = outcome.steps[name]
            console.print(
                f"      [yellow]sub-step {name} missed[/yellow] "
                f"{'· ' + '; '.join(sub.notes) if sub.notes else ''}"
            )

    agents = (
        ", ".join(f"{a.name}={a.effective_model}" for a in result.agents)
        or "none found"
    )
    console.print(f"\n  [dim]agents:  {agents}[/dim]")
    console.print(f"  [dim]models:  {', '.join(result.models)}[/dim]")

    if result.broken:
        console.print()
        warn(f"{len(result.broken)} patch(es) no longer match. Anchor counts:")
        for check in result.broken:
            anchors = result.anchors.get(check.patch.id, {})
            for anchor, count in anchors.items():
                colour = "red" if count == 0 else "dim"
                console.print(f"    [{colour}]{count:3d}[/{colour}]  {anchor}")
        console.print(
            "\n  [dim]A 0 next to an anchor is where upstream moved. "
            "See docs/PLAYBOOK.md to repair.[/dim]"
        )
        return 1
    ok("All patches still match this build.")
    return 0


def cmd_list(args) -> int:
    heading("Available patches")
    for group, patches in by_group().items():
        if not patches:
            continue
        console.print(f"\n[bold]{group}[/bold]")
        for patch in patches:
            tag = "[dim](via --model)[/dim]" if not patch.default else ""
            console.print(f"  [cyan]{patch.id:18s}[/cyan] {patch.summary} {tag}")

    install = locate.find()
    if install is None:
        return 0
    try:
        source = patcher.read_pristine(install).source
    except (BunError, OSError):
        return 0
    agents = discover_agents(source)
    if agents:
        console.print(f"\n[bold]Subagents in Claude {install.version or '?'}[/bold]")
        for agent in agents:
            console.print(
                f"  [cyan]{agent.name:18s}[/cyan] default model: {agent.effective_model}"
            )
        console.print(
            f"  [dim]models: {', '.join([INHERIT, *discover_models(source)])}[/dim]"
        )
    return 0


def cmd_restore(args) -> int:
    install = locate.find_or_raise()
    try:
        restored = patcher.restore(install)
    except FileNotFoundError as exc:
        err(str(exc))
        return 1
    ok(f"Restored {restored} from backup.")
    console.print("[dim]Restart Claude Code for changes to take effect.[/dim]")
    return 0


def cmd_extract(args) -> int:
    from .bun import container

    bundle = container.read(args.path)
    sys.stdout.buffer.write(bundle.source.encode("utf8"))
    sys.stdout.buffer.flush()
    return 0


def cmd_menu(args) -> int:
    from .menu import run_menu

    return run_menu()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch-cc",
        description="Interactive patcher for the Claude Code native binary.",
    )
    parser.set_defaults(func=cmd_menu)
    sub = parser.add_subparsers(dest="command")

    p_apply = sub.add_parser("apply", help="apply patches to the installed binary")
    p_apply.add_argument(
        "patches",
        nargs="*",
        metavar="PATCH",
        help="patch ids to apply (default: the default set)",
    )
    p_apply.add_argument(
        "--brand",
        nargs="?",
        const=_DERIVE,
        metavar="NAME",
        help="custom startup name (no value: <username>'s Code)",
    )
    p_apply.add_argument(
        "--model",
        action="append",
        metavar="AGENT=MODEL",
        help="override a subagent's default model (repeatable)",
    )
    p_apply.add_argument(
        "--suffix", metavar="TEXT", help="--version marker text (default: (patched))"
    )
    p_apply.set_defaults(func=cmd_apply)

    sub.add_parser(
        "status", help="show what is applied to the installed binary"
    ).set_defaults(func=cmd_status)
    sub.add_parser(
        "doctor", help="check every patch still matches this build"
    ).set_defaults(func=cmd_doctor)
    sub.add_parser(
        "list", help="list patches, and the agents/models in your binary"
    ).set_defaults(func=cmd_list)
    sub.add_parser(
        "restore", help="restore the original binary from backup"
    ).set_defaults(func=cmd_restore)

    p_extract = sub.add_parser(
        "extract", help="dump the JS bundle from a binary (debug)"
    )
    p_extract.add_argument("path", help="path to a Claude native binary")
    p_extract.set_defaults(func=cmd_extract)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (FileNotFoundError, BunError) as exc:
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
