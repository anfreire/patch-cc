"""The commands: ``apply`` and ``restore`` for users, ``doctor`` and ``extract``
for whoever maintains the patches. Bare ``patch-cc`` is the menu.

``apply`` has one input, the saved selection, and the menu is its editor:
nothing on the command line changes what it bakes, so the same ``apply``
always bakes the same thing and what it bakes is what the menu shows. Both
read the selection through :func:`cache.seed` -- the saved file, else what the
installed binary records, else the default set -- so the two cannot disagree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.markup import escape

from . import cache, locate, patcher
from .bun import Bundle, BunError
from .js import Source, SyntaxGateError
from .patches import Options, Outcome
from .patches.custom_models import validate_models
from .ui import (
    MARKS,
    applied_value,
    console,
    endpoint_note,
    err,
    findings,
    heading,
    ok,
    verdicts,
    warn,
)


def _saved(installed: Bundle) -> cache.Selection:
    """The selection ``apply`` bakes, read exactly as the menu reads it.

    A saved file that will not parse is refused rather than guessed around:
    this command *acts*, and acting on the binary's record or the defaults
    while claiming to apply a selection is the one thing a replay must never
    do. The menu, which only shows, falls back and says so instead.
    """
    selection, note = cache.seed(patcher.read_manifest(installed.source))
    if note is not None:
        err(f"{note} Fix it, delete it, or choose again in the menu.")
        raise SystemExit(2)
    if selection.dropped_patches:
        # A replay that quietly applies a smaller set than was saved is the one
        # thing a replay must never be. The file is readable and the user's to
        # edit, so it can name an id this tool does not have -- said here, once.
        warn(
            "saved selection names patch id(s) this patch-cc does not have, "
            "skipped: " + ", ".join(selection.dropped_patches)
        )
    return selection


def _for_build(selection: cache.Selection, source: Source) -> tuple[list[str], Options]:
    """The patch input for this build.

    A retired agent or model is `subagent-models`' own to skip, and its report
    says so.
    """
    active = selection.active()
    selected, options = active.patches, active.options
    if "custom-models" in selected:
        # Refused at the front door, before anything is written: a saved id
        # that this (newer) binary now claims for itself would be registered
        # over a real model (docs/PLAYBOOK.md, the collision guard).
        options.custom_models = validate_models(source, options.custom_models)
        if not options.endpoint:
            raise ValueError(
                "saved custom models need an endpoint; set one in the menu"
            )
    return selected, options


def _print_findings(outcome: Outcome) -> None:
    """The detail under a patch line -- worded in :func:`ui.findings`."""
    for style, text in findings(outcome):
        console.print(f"      [{style}]· {escape(text)}[/{style}]")


def _print_report(report: patcher.PatchReport) -> None:
    options = report.options
    heading("Patch results")
    for patch, outcome in report.results:
        mark, colour = MARKS[outcome.health]
        detail = f"  applied {outcome.applied}" if outcome.applied else ""
        if value := applied_value(patch, outcome, options):
            detail += f"  [dim]→ {escape(value)}[/dim]"
        console.print(f"  [{colour}]{mark}[/{colour}] {patch.title:28s}{detail}")
        _print_findings(outcome)

    if report.output is None:
        console.print()
        err("No patch changed anything; the binary was left untouched.")
        console.print("  [dim]Run `patch-cc doctor` for anchor details.[/dim]")
        return

    grown = (report.patched_size - report.original_size) / 1e6
    console.print()
    ok(f"Wrote {report.output}  ({report.patched_size / 1e6:.0f} MB, {grown:+.0f} MB)")
    if report.backup:
        console.print(f"  [dim]backup: {report.backup}[/dim]")
    if "custom-models" in report.landed_ids:
        # The binary now routes custom models to this endpoint. Said here because this
        # is the moment it becomes true, and the run that makes it true is the
        # only one that knows: every later symptom is a Claude Code error naming
        # no cause, minutes after the fact.
        style, note = endpoint_note(options.endpoint)
        console.print(f"  [{style}]endpoint: {note}[/{style}]")
    if report.regressions:
        warn(
            f"{len(report.regressions)} patch(es) did not apply and were left out: "
            + ", ".join(p.id for p in report.regressions)
        )
        console.print("  [dim]Run `patch-cc doctor` for anchor details.[/dim]")


def cmd_apply(args) -> int:
    install = locate.find_or_raise()
    installed, pristine = patcher.read_installation(install)
    selection = _saved(installed)
    selected, options = _for_build(selection, pristine.source)

    version = install.version or "?"
    name = install.binary.name
    where = version if name == version else f"{version} ({name})"
    heading(f"Patching Claude {where}")
    try:
        report = patcher.patch_installation(install, selected, options, bundle=pristine)
    except patcher.AlreadyPatchedError as exc:
        warn(str(exc))
        return 1
    except SyntaxGateError as exc:
        # The final full-parse gate (or a manifest splice) found rubble and
        # refused to write. The install and its backup are untouched -- the gate
        # sits before either is written -- so this is a clean stop, not a
        # half-patched binary, and it is reported as one rather than as a
        # traceback out of `main`.
        err(str(exc))
        return 1
    except BunError as exc:
        err(str(exc))
        return 1

    _print_report(report)
    if report.output is not None:
        # Written back once it is really in the binary, the same rule the menu
        # follows: the file then always describes the last bake, and an id this
        # tool does not have leaves it here. A memory that cannot be written is
        # a note under the report, never an error over it.
        try:
            cache.save(selection)
        except OSError as exc:
            warn(f"selection not remembered: {exc}")
        console.print("\n[dim]Restart Claude Code for changes to take effect.[/dim]")
    return 0 if report.ok else 1


def _doctor_target(path: str | None) -> tuple[Bundle, str] | None:
    """The clean bundle to test and how to label it, or ``None`` if there is none.

    Matcher health is only meaningful against an unpatched bundle: our own edits
    remove the very anchors the matchers look for. An explicit path is taken as
    given -- that is how any kept backup becomes a regression corpus -- while the
    installed binary falls back to its pristine copy when it is already patched.
    """
    from .bun import container

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

    clean = patcher.existing_backup(install)
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
    # An unparseable bundle is refused by `apply`, so there is nothing honest to
    # bake; the defect line below already owns that verdict.
    run = doctor.smoke(test_bundle, result) if result.defect is None else None

    heading(f"Patch health against {label}")
    for patch, outcome in result.results:
        mark, colour = MARKS[outcome.health]
        console.print(
            f"  [{colour}]{mark}[/{colour}] {patch.id:20s} "
            f"cand={outcome.candidates} applied={outcome.applied}"
        )
        _print_findings(outcome)

    agents = ", ".join(f"{a.name}={a.default}" for a in result.agents) or "none found"
    console.print(f"\n  [dim]agents:  {escape(agents)}[/dim]")
    console.print(f"  [dim]models:  {', '.join(result.models)}[/dim]")

    # One verdict, one exit code (`ui.verdicts` / `DryRun.clean` plus the smoke
    # run), so this and the menu cannot disagree -- including the parse defect,
    # which is not a per-patch verdict (no matcher caused it, `apply` refuses
    # the binary outright) but does decide the exit. Green lines get the ✓,
    # cautions the !, detail is indented under them.
    console.print()
    for style, text in verdicts(result, run):
        if style == "green":
            ok(text)
        elif style == "yellow":
            warn(text)
        else:
            console.print(f"    [{style}]{escape(text)}[/{style}]")
    return 0 if result.clean and (run is None or run.ok) else 1


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
    # Every JS module the container carries, each behind a header naming its blob
    # index, joined into one stream. Since 2.1.242 the app is split across many
    # modules; concatenating them keeps the PLAYBOOK repair loop's `rg` working
    # over one file, and the headers say which module a hit lives in. A pre-split
    # build is one module, so this is its bytes behind a single header.
    out: list[bytes] = []
    for index, data in sorted(bundle.source.contents().items()):
        out.append(f"// ==== patch-cc module {index} ====\n".encode())
        out.append(data)
        out.append(b"\n")
    sys.stdout.buffer.write(b"".join(out))
    sys.stdout.buffer.flush()
    return 0


def cmd_menu(args) -> int:
    from .menu import run_menu

    return run_menu()


_MAIN_EPILOG = """\
Run with no arguments to open the menu: pick patches, configure them, apply.

  patch-cc apply           bake the saved selection into the installed binary
                           (after a Claude update, this is all you need)
  patch-cc restore         put the original binary back from backup

maintenance:
  patch-cc doctor [PATH]   check every patch against a clean build, bake and boot it
  patch-cc extract PATH    dump a binary's JS bundle to stdout (the repair loop)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="patch-cc",
        description="Interactive patcher for the Claude Code native binary.",
        epilog=_MAIN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.set_defaults(func=cmd_menu)
    sub = parser.add_subparsers(dest="command")

    sub.add_parser(
        "apply",
        help="bake the saved selection into the installed binary",
        description="Bake the selection the menu would open on -- the saved one, "
        "else what the installed binary records, else the default set -- into "
        "the installed Claude binary. Always starts from a pristine copy, so "
        "re-applying replaces the previous set rather than stacking on it.",
    ).set_defaults(func=cmd_apply)
    sub.add_parser(
        "restore", help="restore the original binary from backup"
    ).set_defaults(func=cmd_restore)

    p_doctor = sub.add_parser(
        "doctor", help="check every patch still matches a build (maintenance)"
    )
    p_doctor.add_argument(
        "path", nargs="?", help="binary to check (default: the installed one)"
    )
    p_doctor.set_defaults(func=cmd_doctor)
    p_extract = sub.add_parser(
        "extract", help="dump the JS bundle from a binary (maintenance)"
    )
    p_extract.add_argument("path", help="path to a Claude native binary")
    p_extract.set_defaults(func=cmd_extract)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (OSError, BunError, ValueError) as exc:
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
