"""The interactive menu shown by bare ``patch-cc``.

A fullscreen frame: fixed title and status on top, fixed key hints at the
bottom, and only the patch list scrolling in between. Every choice is a picker
driven by what the binary itself offers -- agents and models are discovered,
never typed -- and typing exists only where a value is genuinely free text
(the startup name, the --version marker). All configuration happens in
centered modals floating over the dimmed list; the list itself never grows
sub-rows. ``s`` saves, and the same frame then shows the per-patch results.

The engine is deliberately small: ``blessed`` owns the terminal (fullscreen,
cbreak, parsed keystrokes, live size) and Rich owns every pixel drawn. A frame
is composed as Rich segments, a modal is a centered ``Panel`` composited over
the dimmed background, and the whole thing is painted with absolute cursor
moves. There is no widget toolkit, no focus system, and no event bubbling --
one loop, one state machine.

Pre-selection comes from the binary's own manifest when it is patched -- the
binary is the state -- falling back to the cached last selection, then to the
defaults. Nothing is written until the user saves; quitting with unsaved
changes asks first.
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from blessed import Terminal
from rich import box
from rich.align import Align
from rich.console import COLOR_SYSTEMS, Console, Group
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from . import cache, locate, patcher
from .bun import Bundle, BunError, container
from .patches import (
    DEFAULT_BRAND,
    DEFAULT_SUFFIX,
    GROUP_ORDER,
    Options,
    Patch,
    by_group,
    derived_brand,
)
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models
from .ui import console, err

if TYPE_CHECKING:
    from .doctor import DryRun, Status

#: Sentinel choice: leave this agent on its built-in default.
_KEEP = "keep"

#: Anthropic ink-and-paper: terracotta (Claude's coral) is the brand and
#: everything interactive, kraft tan is a value the user wrote in, warm gold
#: is caution, and rules are the edge of the page.
_ACCENT = "#D97757"
_VALUE = "#D4A27F"
_WARN = "#E3B341"
_RULE = "#6f6459"
_PANEL_WIDTH = 72
#: The Claude starburst, breathing — the busy view thinks like Claude does.
_SPINNER = ["·", "✢", "✳", "✺", "✳", "✢"]


def _hints(*pairs: tuple[str, str]) -> Text:
    """Key hints as ``key label`` pairs: the key accented, the label quiet."""
    text = Text()
    for i, (key, label) in enumerate(pairs):
        if i:
            text.append("  ·  ", style="dim")
        text.append(key, style=_ACCENT)
        text.append(f" {label}", style="dim")
    return text


#: Patches whose row opens a modal on enter instead of plain toggling.
_CONFIGURABLE = {"subagent-models", "branding", "version-marker"}


# ----------------------------------------------------------------- rows


@dataclass(slots=True)
class HeaderRow:
    title: str


@dataclass(slots=True)
class PatchRow:
    patch: Patch
    on: bool


@dataclass(slots=True)
class AgentRow:
    """Per-agent override state; edited in the agents modal, never a list row."""

    agent: BuiltinAgent
    #: The chosen override, or ``_KEEP`` for "leave the built-in default".
    choice: str = _KEEP


@dataclass(slots=True)
class TextRow:
    """A free-text value; edited in the input modal, never a list row."""

    key: str
    label: str
    value: str


Row = HeaderRow | PatchRow


@dataclass(slots=True)
class MenuModel:
    """Everything the menu operates on, independent of the rendering engine."""

    install: locate.Installation
    status: "Status"
    pristine: Bundle
    agents: list[BuiltinAgent]
    models: list[str]
    patch_rows: dict[str, PatchRow] = field(default_factory=dict)
    agent_rows: list[AgentRow] = field(default_factory=list)
    text_rows: dict[str, TextRow] = field(default_factory=dict)

    @classmethod
    def build(
        cls, install: locate.Installation, status: "Status", pristine: Bundle
    ) -> "MenuModel":
        agents = discover_agents(pristine.source)
        models = [INHERIT, *discover_models(pristine.source)]
        model = cls(
            install=install,
            status=status,
            pristine=pristine,
            agents=agents,
            models=models,
        )

        seed = model._seed()
        for group in GROUP_ORDER:
            for patch in by_group().get(group, []):
                model.patch_rows[patch.id] = PatchRow(patch, patch.id in seed.patches)

        for agent in agents:
            picked = seed.options.subagent_models.get(agent.name)
            model.agent_rows.append(
                AgentRow(agent, picked if picked in models else _KEEP)
            )

        brand = seed.options.brand if seed.options.rebrands else derived_brand()
        model.text_rows["brand"] = TextRow("brand", "name", brand)
        model.text_rows["suffix"] = TextRow(
            "suffix", "marker", seed.options.version_suffix
        )
        return model

    def _seed(self) -> cache.Selection:
        """Manifest > cached last selection > defaults."""
        manifest = getattr(self.status, "manifest", None)
        if manifest:
            seed = cache.Selection()
            seed.patches = [
                p for p in manifest.get("patches", []) if isinstance(p, str)
            ] or seed.patches
            seed.options = Options(
                brand=manifest.get("brand") or DEFAULT_BRAND,
                version_suffix=manifest.get("suffix") or DEFAULT_SUFFIX,
                subagent_models={
                    a: m
                    for a, m in (manifest.get("models") or {}).items()
                    if isinstance(a, str) and isinstance(m, str)
                },
            )
            return seed
        return cache.load()

    def rows(self) -> list[Row]:
        rows: list[Row] = []
        for group in GROUP_ORDER:
            patches = by_group().get(group, [])
            if not patches:
                continue
            rows.append(HeaderRow(group))
            rows.extend(self.patch_rows[patch.id] for patch in patches)
        return rows

    def overridden(self) -> int:
        return sum(1 for row in self.agent_rows if row.choice != _KEEP)

    # -- what apply would do

    def selection(self) -> cache.Selection:
        selected = [pid for pid, row in self.patch_rows.items() if row.on]
        options = Options()
        overrides = {
            row.agent.name: row.choice for row in self.agent_rows if row.choice != _KEEP
        }
        if "subagent-models" in selected:
            if overrides:
                options.subagent_models = overrides
            else:
                selected.remove("subagent-models")
        if "branding" in selected:
            brand = self.text_rows["brand"].value.strip() or derived_brand()
            if brand == DEFAULT_BRAND:
                selected.remove("branding")
            else:
                options.brand = brand
        if "version-marker" in selected:
            options.version_suffix = (
                self.text_rows["suffix"].value.strip() or DEFAULT_SUFFIX
            )
        return cache.Selection(patches=selected, options=options)


# ----------------------------------------------------------------- modals
#
# A modal is plain state plus two methods: ``handle`` mutates on a key name,
# ``render`` returns the centered Panel. ``finish`` is injected when pushed;
# calling it closes the modal and hands the result to the opener's callback.


class PickModal:
    """A centered list of choices; enter picks, esc closes."""

    def __init__(
        self,
        title: str,
        items: list[str],
        label: Callable[[str, bool], Text],
        *,
        current: str | None = None,
        on_pick: Callable[[str], None] | None = None,
        hint: Text | None = None,
        width: int = 46,
    ) -> None:
        self.title = title
        self.items = items
        self.label = label
        self.on_pick = on_pick
        self.hint = (
            hint if hint is not None else _hints(("enter", "select"), ("esc", "cancel"))
        )
        self.width = width
        self.cursor = items.index(current) if current in items else 0
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        if key in ("down", "j"):
            self.cursor = (self.cursor + 1) % len(self.items)
        elif key in ("up", "k"):
            self.cursor = (self.cursor - 1) % len(self.items)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.items) - 1
        elif key == "enter":
            item = self.items[self.cursor]
            if self.on_pick is not None:
                self.on_pick(item)
            else:
                self.finish(item)
        elif key in ("escape", "q"):
            self.finish(None)

    def render(self) -> Panel:
        inner = self.width - 8
        body = Text()
        for i, item in enumerate(self.items):
            current = i == self.cursor
            line = Text()
            line.append("❯ " if current else "  ", style=_ACCENT)
            line.append_text(self.label(item, current))
            line.truncate(inner, overflow="ellipsis")
            body.append_text(line)
            body.append("\n")
        return Panel(
            Group(body, Align.center(self.hint)),
            box=box.ROUNDED,
            border_style=_ACCENT,
            padding=(1, 3),
            title=Text(self.title, style=f"bold {_ACCENT}"),
            title_align="center",
        )


class InputModal:
    """A centered free-text field; enter saves, esc keeps the old value."""

    def __init__(
        self, title: str, value: str, *, width: int = 52, max_len: int = 48
    ) -> None:
        self.title = title
        self.value = value
        self.cur = len(value)
        self.hint = _hints(("enter", "save"), ("esc", "cancel"))
        self.width = width
        self.max_len = max_len
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        if key == "enter":
            self.finish(self.value)
        elif key == "escape":
            self.finish(None)
        elif key == "left":
            self.cur = max(0, self.cur - 1)
        elif key == "right":
            self.cur = min(len(self.value), self.cur + 1)
        elif key == "home":
            self.cur = 0
        elif key == "end":
            self.cur = len(self.value)
        elif key == "backspace":
            if self.cur:
                self.value = self.value[: self.cur - 1] + self.value[self.cur :]
                self.cur -= 1
        elif key == "delete":
            self.value = self.value[: self.cur] + self.value[self.cur + 1 :]
        else:
            ch = " " if key == "space" else key
            if len(ch) == 1 and ch.isprintable() and len(self.value) < self.max_len:
                self.value = self.value[: self.cur] + ch + self.value[self.cur :]
                self.cur += 1

    def render(self) -> Panel:
        line = Text()
        line.append("❯ ", style=_ACCENT)
        line.append(self.value[: self.cur])
        at = self.value[self.cur : self.cur + 1] or " "
        line.append(at, style="reverse")
        line.append(self.value[self.cur + 1 :])
        return Panel(
            Group(line, Text(""), Align.center(self.hint)),
            box=box.ROUNDED,
            border_style=_ACCENT,
            padding=(1, 3),
            title=Text(self.title, style=f"bold {_ACCENT}"),
            title_align="center",
        )


class ConfirmModal:
    """A centered yes/no question; caution gets an amber frame."""

    def __init__(
        self, question: str, action: str, *, tone: str = _ACCENT, width: int = 52
    ) -> None:
        self.question = question
        self.action = action
        self.tone = tone
        self.width = width
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        if key in ("y", "enter"):
            self.finish(True)
        elif key in ("n", "escape", "q"):
            self.finish(False)

    def render(self) -> Panel:
        return Panel(
            Group(
                Align.center(Text(self.question, style="bold")),
                Text(""),
                Align.center(_hints(("y", self.action), ("n", "cancel"))),
            ),
            box=box.ROUNDED,
            border_style=self.tone,
            padding=(1, 3),
            title=Text(self.action, style=f"bold {self.tone}"),
            title_align="center",
        )


Modal = PickModal | InputModal | ConfirmModal


# ----------------------------------------------------------------- app


class MenuApp:
    """One loop, one state machine: read a key, mutate, repaint."""

    def __init__(
        self,
        model: MenuModel,
        *,
        term: Terminal | None = None,
        rich_console: Console | None = None,
    ) -> None:
        self.term = term if term is not None else Terminal()
        self.console = (
            rich_console if rich_console is not None else Console(force_terminal=True)
        )
        self.model = model
        self.cursor = 0
        self.view = "select"  # select | busy | report | doctor
        self.stack: list[tuple[Modal, Callable[[object], None] | None]] = []
        self.report: patcher.PatchReport | None = None
        self.doctor_result: "DryRun | None" = None
        self.busy_message = ""
        self.flash = ""
        self.exit_code = 0
        self.exit_message: str | None = None
        self._exit: int | None = None
        #: A worker's tagged result, read by the loop: (kind, payload, error).
        self._worker_result: tuple[str, Any, str | None] | None = None
        self._frame = 0
        self._scroll = 0
        self._needs_paint = True
        self._last_size = (0, 0)
        color = self.console.color_system
        self._color_system = COLOR_SYSTEMS.get(color) if color else None
        self._clamp_cursor(0)

    # ---------------------------------------------------- loop

    def run(self) -> int:
        term = self.term
        with term.fullscreen(), term.cbreak(), term.hidden_cursor():
            self._disable_flow_control()
            while self._exit is None:
                size = (term.width, term.height)
                if size != self._last_size:
                    self._needs_paint = True
                if self.view == "busy":
                    self._frame += 1
                    self._needs_paint = True
                if self._needs_paint:
                    self._paint(*size)
                    self._needs_paint = False
                    self._last_size = size
                try:
                    keystroke = term.inkey(timeout=0.12 if self.view == "busy" else 0.4)
                except KeyboardInterrupt:
                    if self.view != "busy":
                        self._on_key("ctrl+c")
                    continue
                if self.view == "busy":
                    self._poll_worker()
                    continue
                key = self._key_name(keystroke)
                if key:
                    self._on_key(key)
        return self._exit if self._exit is not None else 0

    @staticmethod
    def _disable_flow_control() -> None:
        """Free ctrl+s from XOFF so a stray press cannot freeze the screen."""
        try:
            import termios

            fd = sys.stdin.fileno()
            attrs = termios.tcgetattr(fd)
            attrs[0] &= ~(termios.IXON | termios.IXOFF)
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
        except Exception:
            pass

    def _key_name(self, keystroke) -> str:
        if not keystroke:
            return ""
        term = self.term
        if keystroke.is_sequence:
            named = {
                term.KEY_UP: "up",
                term.KEY_DOWN: "down",
                term.KEY_LEFT: "left",
                term.KEY_RIGHT: "right",
                term.KEY_ENTER: "enter",
                term.KEY_ESCAPE: "escape",
                term.KEY_BACKSPACE: "backspace",
                term.KEY_DELETE: "delete",
                term.KEY_HOME: "home",
                term.KEY_END: "end",
            }
            return named.get(keystroke.code, "")
        ch = str(keystroke)
        return {
            "\r": "enter",
            "\n": "enter",
            " ": "space",
            "\x7f": "backspace",
            "\x08": "backspace",
            "\x1b": "escape",
        }.get(ch, ch)

    # ---------------------------------------------------- input

    def _on_key(self, key: str) -> None:
        self._needs_paint = True
        if key == "ctrl+c":
            self.stack.clear()
            self._request_quit()
            return
        if self.stack:
            self.stack[-1][0].handle(key)
            return
        if self.view == "select":
            self._key_select(key)
        elif self.view in ("report", "doctor"):
            if key == "q":
                self._exit = self.exit_code
            elif key in ("enter", "escape", "b"):
                self.view = "select"

    def _key_select(self, key: str) -> None:
        rows = self.model.rows()
        row = rows[self.cursor] if self.cursor < len(rows) else None
        self.flash = ""

        if key in ("q", "escape"):
            self._request_quit()
        elif key in ("down", "j"):
            self._move(1)
        elif key in ("up", "k"):
            self._move(-1)
        elif key == "home":
            self.cursor = 0
            self._clamp_cursor(1)
        elif key == "end":
            self.cursor = len(rows) - 1
            self._clamp_cursor(-1)
        elif key == "space" and isinstance(row, PatchRow):
            row.on = not row.on
        elif key == "enter" and isinstance(row, PatchRow):
            self._activate(row)
        elif key in ("s", "a"):
            self._start_apply()
        elif key == "d":
            self._start_doctor()
        elif key == "r":
            self._confirm_restore()

    def _activate(self, row: PatchRow) -> None:
        """Enter on a patch: toggle plain ones, configure configurable ones."""
        patch_id = row.patch.id
        if patch_id == "subagent-models":
            if not self.model.agent_rows:
                self.flash = "no agents discovered in this bundle"
                return
            row.on = True
            self._open_agents_modal()
        elif patch_id in ("branding", "version-marker"):
            row.on = True
            self._open_text_modal("brand" if patch_id == "branding" else "suffix")
        else:
            row.on = not row.on

    # ---------------------------------------------------- modal flows

    def _push(self, modal: Modal, on_close: Callable[[object], None] | None) -> None:
        def finish(result: object) -> None:
            self.stack.pop()
            self._needs_paint = True
            if on_close is not None:
                on_close(result)

        modal.finish = finish
        self.stack.append((modal, on_close))

    def _open_agents_modal(self) -> None:
        model = self.model
        rows = {row.agent.name: row for row in model.agent_rows}
        pad = max(len(name) for name in rows) + 2

        def label(name: str, selected: bool) -> Text:
            row = rows[name]
            text = Text(f"{name:<{pad}}", style="bold" if selected else "")
            if row.choice == _KEEP:
                text.append(f"keep ({row.agent.effective_model})", style="dim")
            else:
                text.append(row.choice, style=_VALUE)
            return text

        def pick(name: str) -> None:
            self._open_model_modal(rows[name])

        self._push(
            PickModal(
                "Subagent models",
                list(rows),
                label,
                on_pick=pick,
                width=52,
                hint=_hints(("enter", "choose model"), ("esc", "done")),
            ),
            None,
        )

    def _open_model_modal(self, agent_row: AgentRow) -> None:
        items = [_KEEP, *self.model.models]

        def label(value: str, _selected: bool) -> Text:
            if value == _KEEP:
                return Text(f"keep default ({agent_row.agent.effective_model})")
            if value == INHERIT:
                return Text("inherit (main model)")
            return Text(value)

        def picked(choice: object) -> None:
            if isinstance(choice, str):
                agent_row.choice = choice

        self._push(
            PickModal(
                f"Model for {agent_row.agent.name}",
                items,
                label,
                current=agent_row.choice,
                width=46,
            ),
            picked,
        )

    def _open_text_modal(self, key: str) -> None:
        row = self.model.text_rows[key]
        titles = {"brand": "Startup name", "suffix": "--version marker"}

        def entered(value: object) -> None:
            if isinstance(value, str) and value.strip():
                row.value = value.strip()

        self._push(InputModal(titles[key], row.value), entered)

    def _confirm_restore(self) -> None:
        def answered(restore: object) -> None:
            if restore:
                self._start_restore()

        self._push(
            ConfirmModal(
                "Restore the original binary from backup?", "restore", tone=_WARN
            ),
            answered,
        )

    def _request_quit(self) -> None:
        if self.view == "select" and self._unsaved():

            def answered(quit_anyway: object) -> None:
                if quit_anyway:
                    self._exit = 0

            self._push(
                ConfirmModal("Quit without saving your changes?", "quit", tone=_WARN),
                answered,
            )
            return
        self._exit = self.exit_code if self.view in ("report", "doctor") else 0

    def _unsaved(self) -> bool:
        """Does the current selection differ from what the binary carries?"""
        manifest = self.model.status.manifest
        sel = self.model.selection()
        if not manifest:
            return bool(sel.patches)
        if set(sel.patches) != set(self.model.status.patch_ids):
            return True
        brand = manifest.get("brand") or DEFAULT_BRAND
        chosen = sel.options.brand if "branding" in sel.patches else DEFAULT_BRAND
        if brand != chosen:
            return True
        if (manifest.get("suffix") or DEFAULT_SUFFIX) != sel.options.version_suffix:
            return True
        return (manifest.get("models") or {}) != sel.options.subagent_models

    # ---------------------------------------------------- actions

    def _start_apply(self) -> None:
        selection = self.model.selection()
        if not selection.patches:
            self.flash = "nothing selected — toggle at least one patch"
            return

        def confirmed(save: object) -> None:
            if not save:
                return
            cache.save(selection)
            self._busy(f"Patching Claude {self.model.install.version or '?'} …")
            threading.Thread(
                target=self._apply_worker, args=(selection,), daemon=True
            ).start()

        count = len(selection.patches)
        self._push(
            ConfirmModal(
                f"Patch Claude {self.model.install.version or '?'} "
                f"with {count} patch{'es' if count != 1 else ''}?",
                "save",
            ),
            confirmed,
        )

    def _apply_worker(self, selection: cache.Selection) -> None:
        try:
            report = patcher.patch_installation(
                self.model.install,
                selection.patches,
                selection.options,
                bundle=self.model.pristine,
            )
            status = None
            if report.output is not None:
                # The binary just changed; recompute the header state off-loop.
                try:
                    from . import doctor

                    status = doctor.status(
                        container.read(str(self.model.install.binary))
                    )
                except (BunError, OSError):
                    status = None
            self._worker_result = ("apply", (report, status), None)
        except (patcher.AlreadyPatchedError, BunError, OSError) as exc:
            self._worker_result = ("apply", None, str(exc))

    def _start_doctor(self) -> None:
        self._busy("Checking every patch against a clean bundle …")
        threading.Thread(target=self._doctor_worker, daemon=True).start()

    def _doctor_worker(self) -> None:
        from . import doctor

        self._worker_result = ("doctor", doctor.dryrun(self.model.pristine), None)

    def _start_restore(self) -> None:
        self._busy("Restoring the original binary …")
        threading.Thread(target=self._restore_worker, daemon=True).start()

    def _restore_worker(self) -> None:
        try:
            patcher.restore(self.model.install)
            self._worker_result = ("restore", None, None)
        except (FileNotFoundError, OSError) as exc:
            self._worker_result = ("restore", None, str(exc))

    def _busy(self, message: str) -> None:
        self.view = "busy"
        self.busy_message = message
        self._needs_paint = True

    def _poll_worker(self) -> None:
        result = self._worker_result
        if result is None:
            return
        self._worker_result = None
        self._needs_paint = True
        kind, payload, error = result

        if kind == "apply":
            if error is not None:
                self.exit_code = 1
                self.flash = error
                self.view = "select"
                return
            report, status = payload
            self.report = report
            self.exit_code = 0 if report.output is not None else 1
            if status is not None:
                self.model.status = status
            self.view = "report"
        elif kind == "doctor":
            self.doctor_result = payload
            self.exit_code = 1 if payload.broken else 0
            self.view = "doctor"
        elif kind == "restore":
            if error is not None:
                self.flash = error
                self.view = "select"
                return
            self.exit_message = "Restored the original binary. Restart Claude Code."
            self._exit = 0

    # ---------------------------------------------------- movement

    @staticmethod
    def _interactive(rows: list[Row]) -> list[int]:
        return [i for i, row in enumerate(rows) if not isinstance(row, HeaderRow)]

    def _clamp_cursor(self, direction: int) -> None:
        rows = self.model.rows()
        targets = self._interactive(rows)
        if not targets:
            self.cursor = 0
            return
        if self.cursor in targets and direction == 0:
            return
        if direction >= 0:
            after = [i for i in targets if i >= self.cursor]
            self.cursor = after[0] if after else targets[-1]
        else:
            before = [i for i in targets if i <= self.cursor]
            self.cursor = before[-1] if before else targets[0]

    def _move(self, delta: int) -> None:
        rows = self.model.rows()
        targets = self._interactive(rows)
        if not targets:
            return
        if self.cursor not in targets:
            self._clamp_cursor(delta)
            return
        index = targets.index(self.cursor)
        self.cursor = targets[max(0, min(len(targets) - 1, index + delta))]

    # ---------------------------------------------------- rendering

    def _paint(self, width: int, height: int) -> None:
        lines = self._compose(width, height)
        term = self.term
        out: list[str] = []
        for y, segments in enumerate(lines):
            out.append(term.move_xy(0, y))
            out.append(self._ansi(segments))
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _ansi(self, segments: list[Segment]) -> str:
        color_system = self._color_system
        parts: list[str] = []
        for segment in segments:
            if segment.control:
                continue
            if segment.style and color_system is not None:
                parts.append(
                    segment.style.render(segment.text, color_system=color_system)
                )
            else:
                parts.append(segment.text)
        return "".join(parts)

    def _line_segments(self, text: Text, width: int) -> list[Segment]:
        options = self.console.options.update_dimensions(width, 1)
        return self.console.render_lines(text, options, pad=True)[0]

    def _compose(self, width: int, height: int) -> list[list[Segment]]:
        if width < 44 or height < 12:
            notice = Text("terminal too small — need at least 44×12", style="yellow")
            lines = [Text("")] * (height // 2) + [_center(notice, width)]
            lines += [Text("")] * (height - len(lines))
            return [self._line_segments(line, width) for line in lines[:height]]

        panel_width = min(_PANEL_WIDTH, width - 4)
        pad = (width - panel_width) // 2

        head = self._head(panel_width)
        foot = self._foot(panel_width)
        body_height = max(1, height - len(head) - len(foot))
        body, cursor_line = self._body(panel_width)

        # Keep the cursor line inside the visible slice, one line of margin.
        if cursor_line is not None:
            if cursor_line < self._scroll + 1:
                self._scroll = max(0, cursor_line - 1)
            elif cursor_line > self._scroll + body_height - 2:
                self._scroll = cursor_line - body_height + 2
        self._scroll = max(0, min(self._scroll, max(0, len(body) - body_height)))
        visible = body[self._scroll : self._scroll + body_height]
        visible += [Text("")] * (body_height - len(visible))

        seg_lines: list[list[Segment]] = []
        for text in (*head, *visible, *foot):
            text.truncate(panel_width, overflow="ellipsis")
            line = Text(" " * pad)
            line.append_text(text)
            seg_lines.append(self._line_segments(line, width))
        seg_lines = seg_lines[:height]

        if self.stack:
            dim = Style(dim=True)
            seg_lines = [
                list(Segment.apply_style(line, post_style=dim)) for line in seg_lines
            ]
            seg_lines = self._overlay(seg_lines, width, height)
        return seg_lines

    def _overlay(
        self, seg_lines: list[list[Segment]], width: int, height: int
    ) -> list[list[Segment]]:
        modal = self.stack[-1][0]
        modal_width = min(modal.width, width - 4)
        options = self.console.options.update_width(modal_width)
        modal_lines = self.console.render_lines(modal.render(), options, pad=True)
        x0 = (width - modal_width) // 2
        y0 = max(0, (height - len(modal_lines)) // 2)
        for i, modal_line in enumerate(modal_lines):
            y = y0 + i
            if y >= height:
                break
            parts = list(Segment.divide(seg_lines[y], [x0, x0 + modal_width, width]))
            left = parts[0] if parts else []
            right = parts[2] if len(parts) > 2 else []
            seg_lines[y] = [*left, *modal_line, *right]
        return seg_lines

    def _head(self, panel_width: int) -> list[Text]:
        title = Text("✳ patch-cc ✳", style=f"bold {_ACCENT}")
        return [
            Text(""),
            _center(title, panel_width),
            _center(self._status_line(), panel_width),
            Text("─" * panel_width, style=_RULE),
        ]

    def _status_line(self) -> Text:
        model = self.model
        line = Text()
        line.append(f"Claude {model.install.version or '?'}", style="bold")
        line.append("  ·  ", style="dim")
        if model.status.patched:
            applied = len(model.status.patch_ids)
            line.append("patched", style="green")
            if applied:
                line.append(f" ({applied})", style="dim")
            if self.view == "select" and self._unsaved():
                line.append("  ·  ", style="dim")
                line.append("unsaved", style=_WARN)
        else:
            line.append("not patched", style=_WARN)
        return line

    def _foot(self, panel_width: int) -> list[Text]:
        lines = [Text("─" * panel_width, style=_RULE)]
        if self.view == "select":
            if self.flash:
                lines.append(_center(Text(self.flash, style=_WARN), panel_width))
            rows = self.model.rows()
            row = rows[self.cursor] if self.cursor < len(rows) else None
            if isinstance(row, PatchRow) and row.patch.id in _CONFIGURABLE:
                context = _hints(("enter", "configure"), ("space", "toggle"))
            else:
                context = _hints(("enter", "toggle"))
            lines.append(_center(context, panel_width))
            lines.append(
                _center(
                    _hints(
                        ("s", "save"), ("d", "doctor"), ("r", "restore"), ("q", "quit")
                    ),
                    panel_width,
                )
            )
        elif self.view in ("report", "doctor"):
            lines.append(_center(_hints(("enter", "back"), ("q", "quit")), panel_width))
        else:
            lines.append(Text(""))
        lines.append(Text(""))
        return lines

    def _body(self, panel_width: int) -> tuple[list[Text], int | None]:
        return {
            "select": self._body_select,
            "busy": self._body_busy,
            "report": self._body_report,
            "doctor": self._body_doctor,
        }[self.view](panel_width)

    def _body_select(self, panel_width: int) -> tuple[list[Text], int | None]:
        lines: list[Text] = []
        cursor_line: int | None = None
        for i, row in enumerate(self.model.rows()):
            if isinstance(row, HeaderRow):
                if lines:
                    lines.append(Text(""))
                lines.append(Text(f"  {row.title.upper()}", style="bold dim"))
                continue
            current = i == self.cursor
            if current:
                cursor_line = len(lines)
            line = Text()
            line.append("❯ " if current else "  ", style=_ACCENT)
            mark, mark_style = ("●", _ACCENT) if row.on else ("○", f"dim {_ACCENT}")
            line.append(f"{mark} ", style=mark_style)
            line.append(
                row.patch.title, style="bold" if current else ("" if row.on else "dim")
            )
            note = self._row_note(row)
            if note is not None:
                gap = panel_width - line.cell_len - note.cell_len
                if gap < 2:
                    note.truncate(max(0, note.cell_len + gap - 2), overflow="ellipsis")
                    gap = panel_width - line.cell_len - note.cell_len
                line.append(" " * max(2, gap))
                line.append_text(note)
            lines.append(line)
        return lines, cursor_line

    def _row_note(self, row: PatchRow) -> Text | None:
        """The current configuration, shown on the row itself when enabled."""
        if not row.on:
            return None
        model = self.model
        if row.patch.id == "subagent-models":
            count = model.overridden()
            if not count:
                return Text("defaults", style="dim")
            return Text(f"{count} override{'s' if count != 1 else ''}", style=_VALUE)
        if row.patch.id == "branding":
            return Text(model.text_rows["brand"].value, style=_VALUE)
        if row.patch.id == "version-marker":
            return Text(model.text_rows["suffix"].value, style=_VALUE)
        return None

    def _body_busy(self, panel_width: int) -> tuple[list[Text], int | None]:
        spinner = _SPINNER[self._frame % len(_SPINNER)]
        return [
            Text(""),
            Text(""),
            Text(""),
            _center(Text(self.busy_message, style="bold"), panel_width),
            Text(""),
            _center(Text(spinner, style=f"bold {_ACCENT}"), panel_width),
        ], None

    def _body_report(self, panel_width: int) -> tuple[list[Text], int | None]:
        lines: list[Text] = []
        report = self.report
        if report is None:
            return lines, None
        for patch, outcome in report.results:
            missed = outcome.missed_steps()
            if outcome.landed and not missed:
                mark, style = "✓", "green"
            elif outcome.landed:
                mark, style = "~", "yellow"
            else:
                mark, style = "✗", "red"
            line = Text()
            line.append(f"  {mark} ", style=style)
            line.append(f"{patch.title:<32}")
            line.append(f"{outcome.applied or '':>3}", style="dim")
            lines.append(line)
            for name in missed:
                lines.append(Text(f"      sub-step missed: {name}", style="yellow"))

        lines.append(Text(""))
        if report.output is None:
            line = Text()
            line.append("  ✗ ", style="red")
            line.append("No patch changed anything; binary left untouched.")
            lines.append(line)
        else:
            saved = (report.original_size - report.patched_size) / 1e6
            line = Text()
            line.append("  ✓ ", style="green")
            line.append(f"Saved to {report.output.name}", style="bold")
            line.append(
                f"  ·  {report.patched_size / 1e6:.0f} MB ({saved:.0f} MB smaller)",
                style="dim",
            )
            lines.append(line)
            lines.append(Text("    Restart Claude Code to see it.", style="dim"))
        return lines, None

    def _body_doctor(self, panel_width: int) -> tuple[list[Text], int | None]:
        lines: list[Text] = []
        result = self.doctor_result
        if result is None:
            return lines, None
        for check in result.checks:
            outcome = check.outcome
            missed = outcome.missed_steps()
            if outcome.landed and not missed:
                mark, style = "✓", "green"
            elif outcome.landed:
                mark, style = "~", "yellow"
            else:
                mark, style = "✗", "red"
            line = Text()
            line.append(f"  {mark} ", style=style)
            line.append(f"{check.patch.id:<22}")
            line.append(
                f"cand={outcome.candidates:<3} applied={outcome.applied}", style="dim"
            )
            lines.append(line)
        lines.append(Text(""))
        lines.append(
            Text(f"  agents  {', '.join(a.name for a in result.agents)}", style="dim")
        )
        lines.append(Text(f"  models  {', '.join(result.models)}", style="dim"))
        return lines, None


def _center(text: Text, width: int) -> Text:
    pad = max(0, (width - text.cell_len) // 2)
    line = Text(" " * pad)
    line.append_text(text)
    return line


# ----------------------------------------------------------------- entry


def run_menu() -> int:
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        err("The interactive menu needs a terminal; use the subcommands instead.")
        console.print("  [dim]patch-cc apply --help[/dim]")
        return 2

    install = locate.find()
    if install is None:
        err("No Claude Code native install found.")
        console.print(
            "  Install it with: [cyan]curl -fsSL https://claude.ai/install.sh | bash[/cyan]"
        )
        return 1

    try:
        installed = container.read(str(install.binary))
        pristine = patcher.read_pristine(install)
    except BunError as exc:
        err(str(exc))
        return 1

    from . import doctor

    status = doctor.status(installed)

    model = MenuModel.build(install, status, pristine)
    app = MenuApp(model)
    code = app.run()
    if app.exit_message:
        console.print(app.exit_message)
    return code
