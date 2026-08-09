"""Command-line entry point.

Bare ``patch-cc`` opens the interactive menu. Every action is also a
subcommand so nothing needs the TUI:

    patch-cc apply [PATCH ...] [--brand [NAME]] [--model AGENT=MODEL]
                   [--suffix TEXT] [--org-label [TEXT]]
                   [--codex MODEL] [--codex-port N]
    patch-cc status
    patch-cc doctor [PATH]       # PATH: check any binary, e.g. an old backup
    patch-cc list
    patch-cc restore
    patch-cc codex ...           # the OpenAI/Codex bridge (see below)
    patch-cc extract PATH        # dump the JS bundle (debugging)

Patch ids are positional; nothing selected means the default set. Every
configurable patch rides on its option: ``--brand`` selects branding, ``--model``
selects subagent-models, ``--codex`` selects codex-models. Agents and models are
validated against what the installed binary itself offers, and Codex ids against
what your plan offers when it can be reached.

``codex`` is the bridge's own face, and only that: ``login``/``logout`` for your
ChatGPT plan, ``serve`` for the localhost gateway, and a bare ``codex`` for its
status. *Which* Codex models to use, and on which port, is patch configuration
like any other -- named above, or picked in the menu -- so nothing under ``codex``
writes patch state. ``serve`` and ``status`` only read the port back out of the
binary that bakes it, which is the one place it is recorded.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rich.markup import escape

from . import cache, locate, patcher
from .bun import BunError, Bundle
from .codex import DEFAULT_PORT, is_valid_port
from .codex.models import CodexModel, catalogue, is_reserved_id, is_valid_id, reconcile
from .patches import (
    DEFAULT_BRAND,
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
from .ui import (
    MARKS,
    applied_value,
    console,
    err,
    findings,
    gateway_note,
    heading,
    ok,
    warn,
)

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
    if patch.id == "codex-models" and opts.codex_models:
        picks = ", ".join(m.id for m in opts.codex_models)
        parts.append(f"cached {picks}  ·  port {opts.codex_port}")
    elif patch.id == "branding":
        parts.append(f"default {derived_brand()!r}")
        if opts.rebrands and "branding" in cached.patches:
            parts.append(f"cached {opts.brand!r}")
    elif patch.id == "version-marker":
        parts.append(f"default {DEFAULT_SUFFIX!r}")
        if opts.version_suffix != DEFAULT_SUFFIX:
            parts.append(f"cached {opts.version_suffix!r}")
    elif patch.id == "org-label":
        parts.append("default hides it")
        if "org-label" in cached.patches:
            parts.append(
                f"cached {opts.org_label!r}" if opts.org_label else "cached hidden"
            )
    elif patch.id == "subagent-models" and opts.subagent_models:
        picks = ", ".join(f"{a}={m}" for a, m in opts.subagent_models.items())
        parts.append(f"cached {picks}")
    return "  ·  ".join(parts)


def _offered_models(source: str, codex_ids: list[str] | None = None) -> list[str]:
    """Every model a subagent can be pinned to in the binary we are about to write.

    ``inherit``, the aliases this bundle already ships, and any Codex model this
    same run registers. The Codex ids are added explicitly because ``source`` is
    the *pristine* bundle: ``codex-models`` registers them during the run that
    follows, so they are valid for the output and absent from the input. One
    source of truth, so ``--model`` validation and ``--from-cache``
    re-validation never drift apart.
    """
    return [INHERIT, *discover_models(source), *(codex_ids or [])]


def _parse_models(
    specs: list[str], source: str, codex_ids: list[str] | None = None
) -> dict[str, str]:
    """Validate ``AGENT=MODEL`` pairs against what this bundle will offer.

    A Codex id counts alongside the binary's own models exactly when the same
    command line named it with ``--codex``: that flag selects ``codex-models``,
    which registers the id before ``subagent-models`` looks for it. If it fails
    to, the pin fails with it -- the patch layer checks the bundle, not this list.
    """
    agents = {a.name: a for a in discover_agents(source)}
    models = _offered_models(source, codex_ids)
    overrides: dict[str, str] = {}
    for spec in specs:
        agent, sep, model = spec.partition("=")
        if not sep or agent not in agents or model not in models:
            err(f"--model expects AGENT=MODEL, got {spec!r}")
            console.print(
                f"  [dim]agents in this binary: {', '.join(sorted(agents))}[/dim]"
            )
            # Codex ids are tagged rather than listed flat: unlabelled,
            # `gpt-5.6-sol` reads as a model this binary ships, which is the one
            # thing this list exists to answer. Same tag the menu's picker uses.
            codex = set(codex_ids or ())
            listed = ", ".join(f"{m} (codex)" if m in codex else m for m in models)
            console.print(f"  [dim]models: {listed}[/dim]")
            raise SystemExit(2)
        overrides[agent] = model
    return overrides


def _codex_selection(model_ids: list[str]) -> list[CodexModel]:
    """The models ``--codex`` named, validated and described, ready to bake.

    An id is the whole of what the flag takes -- everything else about a model is
    derived (:mod:`patch_cc.codex.models`) -- so this is where the two things an id
    alone cannot vouch for get settled: that it is a shape the bundle can safely
    carry, and that your plan actually offers it.

    The plan is asked *best-effort*. Both answers are worth having: the real
    context window (baking a wrong one makes Claude Code auto-compact early) and a
    typo caught now rather than as a mid-session 404. But ``apply`` never *needs*
    the network -- offline or signed out, nothing can be checked and nothing is
    claimed, so the ids stand as typed and bake with their fallbacks.
    """
    for model_id in model_ids:
        if not is_valid_id(model_id):
            err(f"--codex expects a model id, got {model_id!r}")
            console.print("  [dim]lowercase letters, digits, and . _ - only[/dim]")
            raise SystemExit(2)
        if is_reserved_id(model_id):
            err(
                f"{model_id!r} is one of Claude Code's own model names; registering "
                "it would divert that model's own requests to the gateway"
            )
            raise SystemExit(2)
    chosen, missing = reconcile([CodexModel(i) for i in model_ids], _codex_version())
    if missing:
        err(f"your Codex plan does not offer: {', '.join(missing)}")
        console.print("  [dim]`patch-cc apply --help` lists the ones it does[/dim]")
        raise SystemExit(2)
    return chosen


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
    if "branding" in selected and not options.rebrands:
        # Selected but with nothing to rename -- `--brand "Claude Code"`, or a
        # host with no username to derive one from. Said plainly here, the way
        # subagent-models and codex-models say it below, instead of shipping an
        # inert patch that reports itself broken from inside the run.
        err(f"branding needs a name other than {DEFAULT_BRAND!r}: pass --brand NAME")
        raise SystemExit(2)

    if args.suffix:
        options.version_suffix = args.suffix
        if "version-marker" not in selected:
            selected.append("version-marker")

    if args.org_label is not None:
        options.org_label = args.org_label.strip()
        if "org-label" not in selected:
            selected.append("org-label")

    # Order-preserving, and deduplicated: a repeated `--codex` is one model asked
    # for twice, and every array it reaches -- the enum, the routing list, the
    # remembered selection, the manifest `status` reads back -- would otherwise
    # carry it twice and say so. `--model` gets this free from being a dict.
    codex_ids = list(dict.fromkeys(args.codex or ()))
    if args.codex_port is not None:
        options.codex_port = args.codex_port
    # Either Codex flag selects the patch, as --brand and --suffix select theirs:
    # naming a model, or the port to route it to, is asking for the bridge. A port
    # with no model then lands on the same "needs at least one" refusal below as a
    # bare `apply codex-models`, rather than being quietly ignored.
    if (codex_ids or args.codex_port is not None) and "codex-models" not in selected:
        selected.append("codex-models")

    if args.model:
        # A Codex id is a valid pin exactly when this same line named it: --codex
        # selects codex-models, which registers the id before subagent-models looks
        # for it, so the coupling needs no second rule here.
        options.subagent_models = _parse_models(args.model, source, codex_ids)
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

    if "codex-models" in selected:
        if not codex_ids:
            err("codex-models needs at least one --codex MODEL")
            console.print(
                "  [dim]`patch-cc apply --help` lists the models your plan offers[/dim]"
            )
            raise SystemExit(2)
        options.codex_models = _codex_selection(codex_ids)

    return selected, options


def _has_selection_args(args) -> bool:
    """Did the user pass an explicit selection (vs. bare `apply` = the defaults)?

    Only an explicit pick is worth remembering: caching the default set would
    clobber a previously remembered custom selection with nothing meaningful.
    """
    return bool(
        args.patches
        or args.brand is not None
        or args.model
        or args.suffix
        or args.org_label is not None
        or args.codex
        or args.codex_port is not None
    )


def _valid_models(
    models: dict[str, str], source: str, codex_ids: list[str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """Split cached overrides into those this binary still accepts and the rest.

    A build can drop an agent or retire a model between the interactive apply
    that cached the choice and a later ``--from-cache`` replay; those are skipped
    with a warning rather than written blind, mirroring the menu's own check.
    ``codex_ids`` is what this replay will really register, so a cached Codex pin
    survives exactly when its model does.
    """
    known_agents = {a.name for a in discover_agents(source)}
    known_models = set(_offered_models(source, codex_ids))
    valid: dict[str, str] = {}
    dropped: list[str] = []
    for agent, model in models.items():
        if agent in known_agents and model in known_models:
            valid[agent] = model
        else:
            dropped.append(f"{agent}={model}")
    return valid, dropped


def _replayed_codex(models: list[CodexModel]) -> list[CodexModel]:
    """Remembered Codex picks, refreshed -- minus any your plan no longer offers.

    The lenient half of the pair :func:`_codex_selection` opens. A command line
    naming a model your plan lacks is a typo worth refusing; a *remembered*
    selection whose plan has since changed is merely out of date, and a replay that
    died on it would leave no way to re-apply the rest of the set. So those are
    skipped with a warning, exactly as a retired subagent target is.
    """
    if not models:
        return []  # nothing to refresh, and nothing to ask the plan about
    refreshed, missing = reconcile(models, _codex_version())
    if missing:
        warn(
            "cached Codex model(s) your plan no longer offers, skipped: "
            + ", ".join(missing)
        )
    gone = set(missing)
    return [m for m in refreshed if m.id not in gone]


def _from_cache(args, source: str) -> tuple[list[str], Options]:
    """Rebuild the last interactive selection for a non-interactive apply.

    The single place a persisted choice drives an action rather than pre-filling
    the menu -- entered explicitly via ``--from-cache``, so the cache is a named
    argument, not hidden state (see docs/CONDUCT.md). Model overrides are
    re-validated against the binary in hand.
    """
    if _has_selection_args(args):
        err(
            "--from-cache replays your last interactive selection; do not combine "
            "it with patch ids or --brand / --model / --suffix / --org-label / "
            "--codex."
        )
        raise SystemExit(2)
    if not cache.cache_path().exists():
        err(
            "No cached selection yet. Apply once from the interactive menu "
            "(run `patch-cc`), then `--from-cache` replays it."
        )
        raise SystemExit(2)
    if cache.load_strict() is None:
        # A cache that will not parse has no selection in it. `cache.load` answers
        # with the *default set* so the menu always has something to pre-check --
        # right for a pre-fill, wrong for the one flag that acts on it: this would
        # silently apply the defaults while claiming to replay what you saved.
        err(
            f"Cached selection at {cache.cache_path()} is unreadable, so there is "
            "nothing to replay. Re-save it from the interactive menu (run "
            "`patch-cc`), or name the patches you want."
        )
        raise SystemExit(2)

    selection = cache.load()
    options = selection.options
    selected = list(selection.patches)

    # Codex rides the remembered selection like every other choice, so a replay
    # bakes exactly what was saved. Ids left behind by a selection that did *not*
    # name the patch are orphans -- dropped, never a reason to add it back, the
    # same rule the subagent pins follow below (docs/CONDUCT.md).
    if "codex-models" in selected:
        options.codex_models = _replayed_codex(options.codex_models)
        if not options.codex_models:
            # Said rather than merely done: the saved selection named this patch, so
            # its silent absence from the report would be the one thing a replay
            # must never be -- quiet about not replaying something.
            warn("no Codex model left to register; codex-models skipped")
            selected.remove("codex-models")
    else:
        options.codex_models = []
    codex_ids = [m.id for m in options.codex_models]

    if options.subagent_models:
        valid, dropped = _valid_models(options.subagent_models, source, codex_ids)
        options.subagent_models = valid
        if dropped:
            warn(
                "cached model override(s) not valid for this build, skipped: "
                + ", ".join(dropped)
            )
        if not valid and "subagent-models" in selected:
            selected.remove("subagent-models")
        elif "subagent-models" not in selected:
            # The saved selection left the patch out, so the pins riding in the
            # same file are orphans -- drop them rather than add the patch back.
            # A replay replays a declared choice and never revises it
            # (docs/CONDUCT.md); adding it here made `--from-cache` apply a patch
            # the selection it claims to be replaying deliberately excluded.
            options.subagent_models = {}
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
        if value := applied_value(patch, outcome, options):
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
    if "codex-models" in report.landed_ids:
        # The binary now routes Codex models to this port. Said here because this
        # is the moment it becomes true, and the run that makes it true is the
        # only one that knows: every later symptom is a Claude Code error naming
        # no cause, minutes after the fact.
        style, note = gateway_note(options.codex_port)
        console.print(f"  [{style}]gateway: {note}[/{style}]")
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
        if "org" in st.manifest:
            console.print(f"  org:       {st.manifest['org'] or '(hidden)'}")
        for agent, model in (st.manifest.get("models") or {}).items():
            console.print(f"  model:     {agent} = {model}")
        codex = st.manifest.get("codex")
        if isinstance(codex, dict):
            for entry in codex.get("models") or []:
                if isinstance(entry, str):
                    console.print(f"  codex:     [cyan]{escape(entry)}[/cyan]")
            # The port the redirect was baked with -- probed, not merely named:
            # this is the address the binary sends to, so "is anything there?" is
            # the question being asked. It is also the only record of it, which is
            # why `codex serve` and `codex status` read it from right here rather
            # than keeping a copy that could disagree.
            if isinstance(codex.get("port"), int):
                style, note = gateway_note(codex["port"])
                console.print(f"  gateway:   [{style}]{note}[/{style}]")
    elif st.patched:
        console.print(
            "  [dim]patched by an older patch-cc (no manifest); "
            "re-apply to record one[/dim]"
        )
    console.print(
        f"  bytecode:  "
        f"{'stripped' if st.bytecode_stripped else f'{st.bytecode_size / 1e6:.0f} MB present'}"
    )
    if backup := patcher.existing_backup(install):
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
                console.print(f"  [dim]{'':18s} {escape(hint)}[/dim]")
    return 0


def cmd_restore(args) -> int:
    install = locate.find_or_raise()
    try:
        restored = patcher.restore(install)
    except FileNotFoundError as exc:
        err(str(exc))
        return 1
    if restored is None:
        # Not an error: the binary is already what was asked for. Warned rather
        # than reported as success, because the thing that did *not* happen -- a
        # copy-back that could have downgraded Claude -- is the point.
        warn(
            f"{install.binary} is not patched, so there is nothing to restore. "
            "The kept copy is of whichever version was patched last, and putting "
            "it back could replace a newer Claude with an older one."
        )
        return 0
    ok(f"Restored {restored} from backup.")
    console.print("[dim]Restart Claude Code for changes to take effect.[/dim]")
    return 0


def cmd_extract(args) -> int:
    from .bun import container

    bundle = container.read(args.path)
    sys.stdout.buffer.write(bundle.source.encode("utf8"))
    sys.stdout.buffer.flush()
    return 0


# ----------------------------------------------------------------- codex
#
# The bridge's non-interactive face, and only the bridge: signing in, running the
# gateway, and saying whether either is ready. *Which* Codex models to use, and on
# which port, is patch configuration -- `apply --codex`, or the menu -- so nothing
# here writes patch state. These two read the port back out of the binary that
# bakes it, which is reading the one truth, not keeping a second copy of it.


def _codex_version() -> str:
    install = locate.find()
    return (install.version or "") if install else ""


def _baked_port() -> int | None:
    """The port the installed binary really routes Codex models to, if any.

    Read from the manifest, because that is where the answer *is*: the redirect was
    compiled with one port, and a copy kept anywhere else could only ever come to
    disagree with it. ``None`` when there is no install, no manifest, or no Codex in
    it -- nothing has been baked, so there is nothing to read and the default
    stands.

    Costs about 0.14s (a 114 MB binary, a 22 MB bundle, one reverse find), which is
    why ``serve`` asks once at startup rather than per request -- and why a re-bake
    onto a different port needs a ``serve`` restart, as the apply report says.
    """
    from .bun import container  # noqa: PLC0415

    install = locate.find()
    if install is None:
        return None
    try:
        manifest = patcher.read_manifest(container.read(str(install.binary)).source)
    except (BunError, OSError):
        return None
    codex = (manifest or {}).get("codex")
    port = codex.get("port") if isinstance(codex, dict) else None
    return port if is_valid_port(port) else None


def cmd_codex_login(args) -> int:
    from .codex import oauth

    def show(url: str, code: str) -> None:
        console.print(f"  Open [cyan]{url}[/cyan] and enter code: [bold]{code}[/bold]")
        console.print("  [dim]waiting for authorization…[/dim]")

    try:
        creds = oauth.login(show)
    except (oauth.OAuthError, OSError) as exc:
        err(str(exc))
        return 1
    ok("Signed in" + (f" ({creds.account_id})" if creds.account_id else "") + ".")
    # Both ways in, because signing in cannot know which one you want, and neither
    # is a step this command could have taken for you. `apply --help` is where your
    # plan's models are listed, so the id the second one needs is one command away.
    console.print(
        "  [dim]next: pick models in `patch-cc`, or "
        "`patch-cc apply --codex <id>` (`apply --help` lists them)[/dim]"
    )
    return 0


def cmd_codex_logout(args) -> int:
    from .codex import oauth

    ok("Signed out." if oauth.logout() else "Was not signed in.")
    return 0


def cmd_codex_serve(args) -> int:
    from .codex import gateway

    baked = _baked_port()
    port = args.port or baked or DEFAULT_PORT

    def listening() -> None:
        """Announced from inside ``serve``, once the socket is really bound."""
        ok(f"Codex gateway on http://127.0.0.1:{port}")
        console.print(
            "  [dim]keep this running; a patched Claude Code routes Codex models "
            "here. ctrl+c to stop.[/dim]"
        )
        if baked is None:
            console.print(
                "  [yellow]![/yellow] [dim]no binary routes here yet — bake with "
                "`patch-cc apply --codex <id>`[/dim]"
            )

    try:
        # The port is the whole of the gateway's configuration: it resolves nothing
        # and holds no model list, so choosing another model needs no restart.
        gateway.serve(port=port, on_ready=listening)
    except OSError as exc:
        err(f"could not start gateway on port {port}: {exc}")
        return 1
    return 0


def cmd_codex_status(args) -> int:
    from .codex import oauth

    creds = oauth.load()
    baked = _baked_port()
    heading("Codex bridge")
    if creds is None:
        signed = "[yellow]no[/yellow]  [dim](patch-cc codex login)[/dim]"
    else:
        signed = "[green]yes[/green]" + (
            f"  [dim]({creds.account_id})[/dim]" if creds.account_id else ""
        )
    # The three facts that have to be true together, in the order you make them
    # true. An account and a running gateway still do nothing if no binary points
    # at them, and that last one is the half a user cannot see. *Which* models it
    # carries is `patch-cc status`'s subject, so it is not restated here.
    routed = (
        "[green]routes Codex models here[/green]"
        if baked is not None
        else "[yellow]nothing baked[/yellow]  [dim](patch-cc apply --codex <id>)[/dim]"
    )
    style, note = gateway_note(baked or DEFAULT_PORT)
    console.print(f"  signed in:  {signed}")
    console.print(f"  gateway:    [{style}]{note}[/{style}]")
    console.print(f"  binary:     {routed}")
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
  patch-cc codex          use OpenAI/Codex models in Claude Code
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
    """Build ``apply --help`` from the registry, the installed binary, *and* your plan.

    Nothing is hardcoded: the patch list and default markers come from the
    registry, the subagent agents and models from the real binary, the Codex ids
    from your ChatGPT plan. This is the one place that catalogue is printed, which
    is why the errors that need it point here instead of fetching it again.

    Every read is deferred to the help action and degrades on its own: no install
    drops the agents/models block, no sign-in drops the Codex line, and nothing
    here can fail the help.
    """
    source, version = _discover_binary()
    agents = discover_agents(source) if source else []
    # What your plan offers, which is exactly the set `--codex` accepts. Nothing is
    # asked of the network unless you are signed in (`catalogue` answers with
    # nothing otherwise), so the common case pays for no request at all.
    offered = [m.id for m in catalogue(version or "")]
    native = _offered_models(source) if source else []
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
        "  --org-label [TEXT]   selects org-label  ·  no value -> hide the segment",
        "  --model AGENT=MODEL  selects subagent-models  ·  repeatable, one per agent",
    ]
    if agents:
        lines += [
            f"      agents (Claude {version or '?'}):  "
            + "  ".join(a.name for a in agents),
            "      models:  " + "  ".join(native),
        ]
    elif source is None:
        lines.append(
            "      (the agents/models list shows when a Claude install is present)"
        )
    lines += [
        "  --codex MODEL        selects codex-models  ·  repeatable, one model id",
        f"  --codex-port N       gateway port for those models  ·  default {DEFAULT_PORT}",
    ]
    if offered:
        # Your plan's own ids, and the whole set --codex takes. Also valid --model
        # targets, but only on a line that names them here too: --codex is what
        # registers the id, so the pin and the registration arrive together.
        lines.append("      your plan offers:  " + "  ".join(offered))
    else:
        lines.append(
            "      (your plan's model ids show here once `patch-cc codex login` "
            "has run)"
        )

    ex_one = f"{agents[0].name}={_example_model(native)}" if agents else "Explore=haiku"
    ex_two = f"{agents[1].name}={_example_model(native, 1)}" if len(agents) > 1 else ""
    ex_codex = offered[0] if offered else "gpt-5.6-sol"

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
        f"  patch-cc apply --codex {ex_codex}",
        "      default set plus that Codex model, routed to the local gateway",
        f"  patch-cc apply --codex {ex_codex} --model {agents[0].name if agents else 'Explore'}={ex_codex}",
        "      ...and that subagent runs on it",
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


def _port(value: str) -> int:
    """A localhost port, rejected at parse time by both flags that take one.

    One home, so ``serve --port`` cannot accept what ``apply --codex-port``
    refuses -- and so ``--port 0`` is refused outright rather than read as "unset"
    and silently replaced by the baked one.
    """
    port = int(value)
    if not is_valid_port(port):
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


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
    p_apply.add_argument(
        "--org-label",
        nargs="?",
        const="",
        metavar="TEXT",
        help="welcome-screen org/email text; selects `org-label` "
        "(no value: hide the segment)",
    )
    p_apply.add_argument(
        "--codex",
        action="append",
        metavar="MODEL",
        help="use an OpenAI/Codex model id; selects `codex-models` (repeatable)",
    )
    p_apply.add_argument(
        "--codex-port",
        type=_port,
        metavar="N",
        help="localhost gateway port those models route to (default: 8817)",
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
        help="describe every patch: ids, descriptions, hints",
        description="Describe every patch: what it does, whether it is on by "
        "default, how to enable it, and the value your last run remembered. Reads "
        "no binary, so it is fast and works anywhere. For apply syntax, examples, "
        "and the agents and models your installed binary offers, see "
        "`apply --help`.",
    ).set_defaults(func=cmd_list)
    sub.add_parser(
        "restore", help="restore the original binary from backup"
    ).set_defaults(func=cmd_restore)

    p_extract = sub.add_parser(
        "extract", help="dump the JS bundle from a binary (debug)"
    )
    p_extract.add_argument("path", help="path to a Claude native binary")
    p_extract.set_defaults(func=cmd_extract)

    p_codex = sub.add_parser("codex", help="use OpenAI/Codex models in Claude Code")
    p_codex.set_defaults(func=cmd_codex_status)
    codex_sub = p_codex.add_subparsers(dest="codex_command")
    codex_sub.add_parser(
        "login", help="sign in to your ChatGPT/Codex plan (device code)"
    ).set_defaults(func=cmd_codex_login)
    codex_sub.add_parser("logout", help="forget stored credentials").set_defaults(
        func=cmd_codex_logout
    )
    p_serve = codex_sub.add_parser("serve", help="run the localhost gateway")
    p_serve.add_argument(
        "--port", type=_port, help="listen port (default: the port your binary bakes)"
    )
    p_serve.set_defaults(func=cmd_codex_serve)
    codex_sub.add_parser(
        "status", help="show sign-in, gateway, and whether the binary routes here"
    ).set_defaults(func=cmd_codex_status)

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
