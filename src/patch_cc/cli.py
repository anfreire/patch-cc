"""Command-line entry point.

Bare ``patch-cc`` opens the interactive menu. Every action is also a
subcommand so nothing needs the TUI:

    patch-cc apply [PATCH ...] [--brand [NAME]] [--model AGENT=MODEL]
                   [--suffix TEXT]
    patch-cc status
    patch-cc doctor [PATH]       # PATH: check any binary, e.g. an old backup
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
from pathlib import Path

from rich.markup import escape

from . import cache, locate, patcher
from .bun import BunError, Bundle
from .patches import (
    DEFAULT_SUFFIX,
    GROUP_ORDER,
    Options,
    Outcome,
    Patch,
    by_group,
    default_ids,
    derived_brand,
    ids,
)
from .patches.agents import INHERIT, discover_agents, discover_models
from .ui import MARKS, applied_value, console, err, findings, heading, ok, warn

#: ``--brand`` with no value: derive the name from the system username.
_DERIVE = ""


def _enable_hint(patch: Patch) -> str:
    """How a patch is turned on: blank for the default set, else how to opt in.

    Reads the patch's own ``option``, so a non-default patch says exactly how it
    is enabled -- ``subagent-models`` needs ``--model``, ``spinner-tips`` is just
    named -- instead of the old blanket ``(via --model)`` that mislabelled every
    off-by-default patch.
    """
    if patch.default:
        return ""
    if patch.option:
        return f"(off by default; enable with {patch.option})"
    return "(off by default; name it to apply)"


def _list_hint(patch: Patch, cached: cache.Selection) -> str:
    """Simple, binary-free hints for `list`: how to enable a patch, its default
    configurable value, and the value your last interactive run cached where
    that is meaningful. The dynamic agent/model catalog lives in `apply --help`.
    """
    parts: list[str] = []
    if enable := _enable_hint(patch):
        parts.append(enable.strip("()"))
    opts = cached.options
    if patch.id == "branding":
        parts.append(f"default {derived_brand()!r}")
        if opts.rebrands and "branding" in cached.patches:
            parts.append(f"cached {opts.brand!r}")
    elif patch.id == "version-marker":
        parts.append(f"default {DEFAULT_SUFFIX!r}")
        if opts.version_suffix != DEFAULT_SUFFIX:
            parts.append(f"cached {opts.version_suffix!r}")
    elif patch.id == "subagent-models" and opts.subagent_models:
        picks = ", ".join(f"{a}={m}" for a, m in opts.subagent_models.items())
        parts.append(f"cached {picks}")
    return "  ·  ".join(parts)


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


def _has_selection_args(args) -> bool:
    """Did the user pass an explicit selection (vs. bare `apply` = the defaults)?

    Only an explicit pick is worth remembering: caching the default set would
    clobber a previously remembered custom selection with nothing meaningful.
    """
    return bool(args.patches or args.brand is not None or args.model or args.suffix)


def _valid_models(
    models: dict[str, str], source: str
) -> tuple[dict[str, str], list[str]]:
    """Split cached overrides into those this binary still accepts and the rest.

    A build can drop an agent or retire a model between the interactive apply
    that cached the choice and a later ``--from-cache`` replay; those are skipped
    with a warning rather than written blind, mirroring the menu's own check.
    """
    known_agents = {a.name for a in discover_agents(source)}
    known_models = {INHERIT, *discover_models(source)}
    valid: dict[str, str] = {}
    dropped: list[str] = []
    for agent, model in models.items():
        if agent in known_agents and model in known_models:
            valid[agent] = model
        else:
            dropped.append(f"{agent}={model}")
    return valid, dropped


def _from_cache(args, source: str) -> tuple[list[str], Options]:
    """Rebuild the last interactive selection for a non-interactive apply.

    The single place a persisted choice drives an action rather than pre-filling
    the menu -- entered explicitly via ``--from-cache``, so the cache is a named
    argument, not hidden state (see docs/CONDUCT.md). Model overrides are
    re-validated against the binary in hand.
    """
    if args.patches or args.brand is not None or args.model or args.suffix:
        err(
            "--from-cache replays your last interactive selection; do not combine "
            "it with patch ids or --brand / --model / --suffix."
        )
        raise SystemExit(2)
    if not cache.cache_path().exists():
        err(
            "No cached selection yet. Apply once from the interactive menu "
            "(run `patch-cc`), then `--from-cache` replays it."
        )
        raise SystemExit(2)

    selection = cache.load()
    options = selection.options
    selected = list(selection.patches)
    if options.subagent_models:
        valid, dropped = _valid_models(options.subagent_models, source)
        options.subagent_models = valid
        if dropped:
            warn(
                "cached model override(s) not valid for this build, skipped: "
                + ", ".join(dropped)
            )
        if not valid and "subagent-models" in selected:
            selected.remove("subagent-models")
    return selected, options


def _print_findings(outcome: Outcome) -> None:
    """The detail under a patch line -- worded in :func:`ui.findings`."""
    for style, text in findings(outcome):
        console.print(f"      [{style}]· {text}[/{style}]")


def _print_report(report: patcher.PatchReport, options: Options) -> None:
    heading("Patch results")
    for patch, outcome in report.results:
        mark, colour = MARKS[outcome.health]
        detail = f"  applied {outcome.applied}" if outcome.applied else ""
        if outcome.applied and (value := applied_value(patch, options)):
            detail += f"  [dim]→ {escape(value)}[/dim]"
        console.print(f"  [{colour}]{mark}[/{colour}] {patch.title:28s}{detail}")
        _print_findings(outcome)

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
            f"{len(report.regressions)} patch(es) did not apply and were left out: "
            + ", ".join(p.id for p in report.regressions)
        )
        console.print("  [dim]Run `patch-cc doctor` for anchor details.[/dim]")


def cmd_apply(args) -> int:
    install = locate.find_or_raise()
    bundle = patcher.read_pristine(install)
    if args.from_cache:
        selected, options = _from_cache(args, bundle.source)
    else:
        selected, options = _requested(args, bundle.source)

    version = install.version or "?"
    name = install.binary.name
    where = version if name == version else f"{version} ({name})"
    heading(f"Patching Claude {where}")
    try:
        report = patcher.patch_installation(install, selected, options, bundle=bundle)
    except patcher.AlreadyPatchedError as exc:
        warn(str(exc))
        return 1
    except BunError as exc:
        err(str(exc))
        return 1

    _print_report(report, options)
    if report.output is not None:
        # Remember an explicit pick so `apply --from-cache` and the menu can
        # replay it -- but never a bare `apply`, which would overwrite a
        # remembered selection with the defaults. `--from-cache` never reaches
        # here with selection args, so a replay does not re-cache itself.
        if _has_selection_args(args):
            cache.save(cache.Selection(patches=selected, options=options))
        console.print("\n[dim]Restart Claude Code for changes to take effect.[/dim]")
    return 0 if report.ok else 1


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


def _doctor_target(path: str | None) -> tuple[Bundle, str] | None:
    """The clean bundle to test and how to label it, or ``None`` if there is none.

    Matcher health is only meaningful against an unpatched bundle: our own edits
    remove the very anchors the matchers look for. An explicit path is taken as
    given -- that is how any kept backup becomes a regression corpus -- while the
    installed binary falls back to its pristine copy when it is already patched.
    """
    from .bun import container  # noqa: PLC0415

    if path is not None:
        # A name the user typed is data, not markup: an unescaped `claude[old]`
        # would have rich swallow the brackets as a style tag and report health
        # against a file that is not the one being tested.
        name = escape(Path(path).name)
        bundle = container.read(path)
        if patcher.is_patched(bundle.source):
            warn(f"{name} is already patched; nothing clean to test.")
            console.print("  [dim]Point doctor at a pristine binary or backup.[/dim]")
            return None
        return bundle, name

    install = locate.find_or_raise()
    bundle = container.read(str(install.binary))
    label = f"Claude {install.version or '?'}"
    if not patcher.is_patched(bundle.source):
        return bundle, label

    clean = patcher.clean_source_path(install)
    if clean is None:
        warn("Installed binary is already patched and no clean backup exists.")
        console.print(
            "  [dim]Matcher health can't be checked against a patched binary. "
            "Run `patch-cc restore`, or test a freshly downloaded binary.[/dim]"
        )
        return None
    return container.read(str(clean)), (
        f"{label}  [dim](installed binary is patched; testing against backup)[/dim]"
    )


def cmd_doctor(args) -> int:
    from . import doctor

    target = _doctor_target(args.path)
    if target is None:
        return 1
    test_bundle, label = target
    result = doctor.dryrun(test_bundle)

    heading(f"Patch health against {label}")
    for patch, outcome in result.results:
        mark, colour = MARKS[outcome.health]
        console.print(
            f"  [{colour}]{mark}[/{colour}] {patch.id:20s} "
            f"cand={outcome.candidates} applied={outcome.applied}"
        )
        _print_findings(outcome)

    agents = (
        ", ".join(f"{a.name}={a.effective_model}" for a in result.agents)
        or "none found"
    )
    console.print(f"\n  [dim]agents:  {agents}[/dim]")
    console.print(f"  [dim]models:  {', '.join(result.models)}[/dim]")

    if result.broken:
        console.print()
        warn(f"{len(result.broken)} patch(es) no longer match. Anchor counts:")
        for patch in result.broken:
            anchors = result.anchors.get(patch.id, {})
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
    """The quick catalog: id, description, simple hints. Registry + cache only --

    no binary read, so it is fast and works anywhere. The dynamic agent/model
    catalog and full usage live in `apply --help`.
    """
    heading("Available patches")
    console.print(
        "  [dim]apply with `patch-cc apply <id>`; "
        "see `apply --help` for flags, examples & your binary's agents/models[/dim]"
    )
    cached = cache.load()
    for group, patches in by_group().items():
        if not patches:
            continue
        console.print(f"\n[bold]{group}[/bold]")
        for patch in patches:
            console.print(f"  [cyan]{patch.id:18s}[/cyan] {patch.summary}")
            if hint := _list_hint(patch, cached):
                console.print(f"  [dim]{'':18s} {hint}[/dim]")
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


_MAIN_EPILOG = """\
Run with no arguments to open the interactive menu.

common tasks:
  patch-cc apply          apply the default patch set to the installed binary
  patch-cc apply --help   every patch id, the flags, and worked examples
  patch-cc status         show exactly what is applied right now
  patch-cc doctor         check every patch still matches this build
  patch-cc list           describe every patch: ids, descriptions, hints
  patch-cc restore        put the original binary back from backup
"""


def _discover_binary() -> tuple[str | None, str | None]:
    """The installed binary's JS source and version, or ``(None, version?)``.

    Best-effort: ``apply --help`` reads the real binary so its agent/model list
    matches what ``--model`` accepts, but a missing or unreadable install just
    drops the dynamic block instead of failing the help.
    """
    install = locate.find()
    if install is None:
        return None, None
    try:
        return patcher.read_pristine(install).source, install.version
    except (BunError, OSError):
        return None, install.version


def _example_model(models: list[str], offset: int = 0) -> str:
    """A concrete (non-inherit) model alias for a ``--model`` example."""
    concrete = [m for m in models if m != INHERIT]
    return concrete[offset % len(concrete)] if concrete else INHERIT


def _apply_epilog() -> str:
    """Build ``apply --help`` from the registry *and* the installed binary.

    Nothing is hardcoded: the patch list and default markers come from the
    registry, the subagent agents and models from the real binary. The binary
    read is deferred to the help action, so only ``apply --help`` pays for it and
    a missing install degrades to the static parts.
    """
    source, version = _discover_binary()
    agents = discover_agents(source) if source else []
    models = [INHERIT, *discover_models(source)] if source else []
    grouped = by_group()

    lines = ["patches  (name them to apply exactly those; * = the default set):", ""]
    for group in GROUP_ORDER:
        group_patches = grouped.get(group, [])
        if not group_patches:
            continue
        lines.append(f"  {group}")
        for patch in group_patches:
            mark = "*" if patch.default else " "
            hint = _enable_hint(patch)
            row = f"    {mark} {patch.id:20s}{patch.title}"
            lines.append(f"{row}   {hint}" if hint else row)
        lines.append("")

    lines += [
        "naming ids replaces the default set with exactly what you name; the",
        "flags below always add their own patch on top of whatever you named.",
        "",
        "configuring patches:",
        f"  --brand [NAME]       selects branding  ·  no value -> {derived_brand()!r}",
        f"  --suffix TEXT        selects version-marker  ·  default {DEFAULT_SUFFIX!r}",
        "  --model AGENT=MODEL  selects subagent-models  ·  repeatable, one per agent",
    ]
    if agents:
        lines += [
            f"      agents (Claude {version or '?'}):  "
            + "  ".join(a.name for a in agents),
            "      models:  " + "  ".join(models),
        ]
    elif source is None:
        lines.append(
            "      (the agents/models list shows when a Claude install is present)"
        )

    ex_one = f"{agents[0].name}={_example_model(models)}" if agents else "Explore=haiku"
    ex_two = f"{agents[1].name}={_example_model(models, 1)}" if len(agents) > 1 else ""

    lines += [
        "",
        "examples:",
        "  patch-cc apply",
        "      the default set (the * patches above)",
        "  patch-cc apply tool-calls live-thinking",
        "      only these two; the default set is replaced",
        "  patch-cc apply --brand",
        f"      the default set, branded {derived_brand()!r}",
        '  patch-cc apply --brand "Ada\'s Code" --suffix "(ada)"',
        "      default set with an explicit startup name and version marker",
        f"  patch-cc apply --model {ex_one}" + (f" --model {ex_two}" if ex_two else ""),
        "      default set plus subagent model override" + ("s" if ex_two else ""),
        "  patch-cc apply --from-cache",
        "      re-apply your last interactive menu selection (saved by `patch-cc`)",
    ]
    return "\n".join(lines)


class _ApplyHelpAction(argparse.Action):
    """``apply -h/--help``: assemble the dynamic epilog, then print help.

    Deferred here rather than at parser-build time so only ``apply --help`` reads
    the binary -- every other ``patch-cc`` invocation stays fast and needs no
    install.
    """

    def __init__(self, option_strings, dest, **kwargs):
        super().__init__(
            option_strings, dest, nargs=0, default=argparse.SUPPRESS, **kwargs
        )

    def __call__(self, parser, namespace, values, option_string=None):
        parser.epilog = _apply_epilog()
        parser.print_help()
        parser.exit()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch-cc",
        description="Interactive patcher for the Claude Code native binary.",
        epilog=_MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(func=cmd_menu)
    sub = parser.add_subparsers(dest="command")

    p_apply = sub.add_parser(
        "apply",
        help="apply patches to the installed binary",
        description="Apply patches to the installed Claude binary. Always starts "
        "from a pristine copy, so re-applying replaces the previous set rather "
        "than stacking on it.",
        add_help=False,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_apply.add_argument(
        "-h",
        "--help",
        action=_ApplyHelpAction,
        help="show this help (reads the installed binary for its agents/models)",
    )
    p_apply.add_argument(
        "patches",
        nargs="*",
        metavar="PATCH",
        help="patch ids to apply (default: the default set; all ids listed below)",
    )
    p_apply.add_argument(
        "--from-cache",
        action="store_true",
        help="re-apply your last interactive menu selection (ignores other args)",
    )
    p_apply.add_argument(
        "--brand",
        nargs="?",
        const=_DERIVE,
        metavar="NAME",
        help="startup name; selects `branding` (no value: <username>'s Code)",
    )
    p_apply.add_argument(
        "--model",
        action="append",
        metavar="AGENT=MODEL",
        help="set a subagent's model; selects `subagent-models` (repeatable)",
    )
    p_apply.add_argument(
        "--suffix",
        metavar="TEXT",
        help="`claude --version` marker text; selects `version-marker` "
        "(default: (patched))",
    )
    p_apply.set_defaults(func=cmd_apply)

    sub.add_parser(
        "status", help="show what is applied to the installed binary"
    ).set_defaults(func=cmd_status)
    p_doctor = sub.add_parser(
        "doctor", help="check every patch still matches this build"
    )
    p_doctor.add_argument(
        "path",
        nargs="?",
        help="binary to check (default: the installed one)",
    )
    p_doctor.set_defaults(func=cmd_doctor)
    sub.add_parser(
        "list",
        help="describe every patch + the subagent models your binary offers",
        description="Describe every patch (what it does, whether it is on by "
        "default, and how to enable it) plus the subagent models discovered in "
        "your installed binary. For apply syntax and examples, see `apply --help`.",
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
    except (OSError, BunError) as exc:
        # Every path argument is a filesystem question, so the whole OSError
        # family (missing, a directory, unreadable) is an answer to report --
        # not a traceback. FileNotFoundError is one of them.
        err(str(exc))
        return 1
    except KeyboardInterrupt:
        console.print("\n[dim]cancelled[/dim]")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
