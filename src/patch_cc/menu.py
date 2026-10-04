"""The interactive menu shown by bare ``patch-cc``.

A fullscreen frame: fixed title and status on top, fixed key hints at the
bottom, and only the patch list scrolling in between. Configuration happens
in centered modals floating over the dimmed list; the
list itself never grows sub-rows. ``s`` applies, and the same frame then shows
the per-patch results.

The engine is deliberately small: ``blessed`` owns the terminal (fullscreen,
cbreak, parsed keystrokes, live size) and Rich owns every pixel drawn. A frame
is composed as Rich segments, a modal is a centered ``Panel`` composited over
the dimmed background, and the whole thing is painted with absolute cursor
moves. There is no widget toolkit, no focus system, and no event bubbling --
one loop, one state machine.

Saved preferences pre-fill the menu, falling back to the binary manifest and
then defaults. The header distinguishes edits in this session from saved changes
awaiting apply. Only edits in this session require confirmation when quitting.
"""

from __future__ import annotations

import sys
import textwrap
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from blessed import Terminal
from rich import box
from rich.align import Align
from rich.console import COLOR_SYSTEMS, Console, Group
from rich.panel import Panel
from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from . import cache, locate, patcher
from . import custom_models as custom
from .bun import Bundle, BunError, container
from .custom_models import CustomModel
from .patches import (
    ALL_PATCHES,
    DEFAULT_BRAND,
    DEFAULT_SUFFIX,
    GROUP_ORDER,
    Options,
    Patch,
    by_group,
    derived_brand,
)
from .patches.agents import INHERIT, BuiltinAgent, discover_agents, discover_models
from .patches.custom_models import claimed_model_names, validate_models
from .ui import (
    MARKS,
    applied_value,
    console,
    endpoint_note,
    err,
    findings,
    verdicts,
)

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


#: Where a key sits on a hint line: whatever else the view offers first, then
#: ``enter``, then the key that leaves it.
_HINT_RANK = {"enter": 1, "esc": 2, "q": 2}


def _hints(*pairs: tuple[str, str]) -> Text:
    """Key hints as ``key label`` pairs: the key accented, the label quiet.

    Ordered here, once -- the view's own keys in the order given, then
    ``enter``, then ``esc``/``q`` -- so every surface reads the same way and
    no view can spell the order its own way.
    """
    text = Text()
    ordered = sorted(pairs, key=lambda pair: _HINT_RANK.get(pair[0], 0))
    for i, (key, label) in enumerate(ordered):
        if i:
            text.append("  ·  ", style="dim")
        text.append(key, style=_ACCENT)
        text.append(f" {label}", style="dim")
    return text


#: Patches whose row opens a modal on enter instead of plain toggling --
#: exactly the ones that carry a configurable value, which is what
#: :attr:`Patch.setting` declares. Derived, so a new such patch cannot be
#: left off this list with its hint reading "enter toggle".
_CONFIGURABLE = {patch.id for patch in ALL_PATCHES if patch.setting is not None}

#: Where `?` sends you from the custom-models submenu: the README section that
#: explains endpoints, keys, windows and ladders. One home for the address.
_CUSTOM_MODELS_HELP = "https://github.com/anfreire/patch-cc#custom-models"


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


@dataclass(slots=True)
class _ModelPick:
    """One saved or discovered model in the draft checklist."""

    model: CustomModel
    on: bool = False


Row = HeaderRow | PatchRow


@dataclass(slots=True)
class MenuModel:
    """Everything the menu operates on, independent of the rendering engine."""

    install: locate.Installation
    status: Status
    pristine: Bundle
    agents: list[BuiltinAgent]
    models: list[str]
    patch_rows: dict[str, PatchRow] = field(default_factory=dict)
    agent_rows: list[AgentRow] = field(default_factory=list)
    text_rows: dict[str, TextRow] = field(default_factory=dict)
    #: Custom models to register -- edited in the
    #: Custom models submenu, seeded and saved exactly like every other choice here.
    custom_models: list[CustomModel] = field(default_factory=list)
    endpoint: str = ""
    pending_key: str | None = field(default=None, repr=False)
    #: Why the saved selection was passed over at seed time, if it was -- shown
    #: once when the menu opens, since the next apply overwrites that file.
    notice: str | None = None

    @classmethod
    def build(
        cls, install: locate.Installation, status: Status, pristine: Bundle
    ) -> MenuModel:
        agents = discover_agents(pristine.source)
        models = [INHERIT, *discover_models(pristine.source)]
        model = cls(
            install=install,
            status=status,
            pristine=pristine,
            agents=agents,
            models=models,
        )

        seed, unreadable = cache.seed(status.manifest)
        if unreadable is not None:
            model.notice = (
                f"{unreadable} The menu shows the binary's current state instead; "
                "applying replaces the file."
            )
        model.custom_models = list(seed.options.custom_models)
        model.endpoint = seed.options.endpoint
        for group in GROUP_ORDER:
            for patch in by_group().get(group, []):
                # A patch this build has no surface for gets no row at all: what
                # the binary cannot support is not offered, the same discovery
                # rule the agent and model pickers follow, and a build that
                # carries the surface again shows the row by itself. A seed
                # (manifest or cache) may still name it -- a saved org-label
                # replayed onto 2.1.246 -- and the selection the screen shows
                # is the selection that runs.
                if patch.absent(pristine.source) is None:
                    model.patch_rows[patch.id] = PatchRow(
                        patch, patch.id in seed.patches
                    )

        # A custom id this run would register is a valid pin too, so a remembered
        # custom override survives instead of snapping back to keep.
        offered = {*models, *custom.model_names(model.custom_models)}
        left_out = [f"patch {pid}" for pid in seed.dropped_patches]
        for agent in agents:
            picked = seed.options.subagent_models.get(agent.name)
            if picked is not None and picked not in offered:
                left_out.append(f"pin {agent.name}={picked}")
                picked = None
            model.agent_rows.append(AgentRow(agent, picked or _KEEP))
        if left_out:
            # What the saved selection asks for that this tool or this build
            # does not offer -- a patch id this tool lacks, a pin to a model the
            # build lacks. Said once, where the replay would warn, and the next
            # apply writes the file without it. The unreadable-file note and
            # this one cannot both apply: nothing was read from that file.
            model.notice = (
                "The saved selection names what this patch-cc or this build "
                "does not offer, so it was left out: "
                + ", ".join(left_out)
                + ". Applying rewrites the saved selection without it."
            )

        brand = seed.options.brand
        model.text_rows["brand"] = TextRow("brand", "name", brand)
        model.text_rows["suffix"] = TextRow(
            "suffix", "marker", seed.options.version_suffix
        )
        model.text_rows["org"] = TextRow("org", "label", seed.options.org_label)
        return model

    def rows(self) -> list[Row]:
        rows: list[Row] = []
        for group in GROUP_ORDER:
            offered = [
                self.patch_rows[patch.id]
                for patch in by_group().get(group, [])
                if patch.id in self.patch_rows
            ]
            if offered:
                rows.append(HeaderRow(group))
                rows.extend(offered)
        return rows

    def overridden(self) -> int:
        return sum(1 for row in self.agent_rows if row.choice != _KEEP)

    def applied_ids(self) -> set[str]:
        """What the binary claims, limited to what this build offers.

        A manifest can name a patch this tool has no row for; counting it would
        leave the selection differing from the binary forever -- a permanent
        "unsaved", and a quit-confirm, on a binary nobody has touched.
        """
        return {pid for pid in self.status.patch_ids if pid in self.patch_rows}

    # -- what apply would do

    def selection(self) -> cache.Selection:
        """All preferences; Selection.active derives what the binary should receive."""
        options = Options(
            brand=self.text_rows["brand"].value.strip() or derived_brand(),
            version_suffix=self.text_rows["suffix"].value.strip() or DEFAULT_SUFFIX,
            org_label=self.text_rows["org"].value.strip(),
            subagent_models={
                row.agent.name: row.choice
                for row in self.agent_rows
                if row.choice != _KEEP
            },
            custom_models=list(self.custom_models),
            endpoint=self.endpoint,
        )
        return cache.Selection(
            [pid for pid, row in self.patch_rows.items() if row.on], options
        )


# ----------------------------------------------------------------- modals
#
# A modal is plain state plus two methods: ``handle`` mutates on a key name,
# ``render`` returns the centered Panel. ``finish`` is injected when pushed;
# calling it closes the modal and hands the result to the opener's callback.


class PickModal:
    """A centered list of choices; enter picks, esc closes."""

    def __init__(
        self,
        title: str | Text | None,
        items: list[str],
        label: Callable[[str, bool], Text],
        *,
        current: str | None = None,
        on_pick: Callable[[str], None] | None = None,
        shortcuts: dict[str, str] | None = None,
        hint: Text | None = None,
        width: int = 46,
        description: Text | None = None,
    ) -> None:
        self.title = title
        self.description = description
        self.items = items
        self.label = label
        self.on_pick = on_pick
        self.shortcuts = shortcuts or {}
        self.hint = (
            hint if hint is not None else _hints(("enter", "select"), ("esc", "cancel"))
        )
        self.width = width
        self.cursor = items.index(current) if current in items else 0
        self.finish: Callable[[object], None] = lambda result: None
        self.error = ""
        self.page_size = len(items)

    def handle(self, key: str) -> None:
        if key in ("down", "j"):
            self.cursor = (self.cursor + 1) % len(self.items)
        elif key in ("up", "k"):
            self.cursor = (self.cursor - 1) % len(self.items)
        elif key == "home":
            self.cursor = 0
        elif key == "end":
            self.cursor = len(self.items) - 1
        elif key == "enter" or key in self.shortcuts:
            item = self.shortcuts.get(key, self.items[self.cursor])
            if self.on_pick is not None:
                self.on_pick(item)
            else:
                self.finish(item)
        elif key in ("escape", "q"):
            self.finish(None)

    def render(self, width: int | None = None, height: int | None = None) -> Panel:
        width = width or self.width
        inner = width - 8
        body = Text()
        if self.description is not None:
            identity = self.description.copy()
            identity.truncate(inner, overflow="ellipsis")
            body.append_text(identity)
            body.append("\n\n")
        start = max(0, self.cursor - self.page_size + 1)
        for i in range(start, min(len(self.items), start + self.page_size)):
            item = self.items[i]
            current = i == self.cursor
            line = Text()
            line.append("❯ " if current else "  ", style=_ACCENT)
            line.append_text(self.label(item, current))
            line.truncate(inner, overflow="ellipsis")
            body.append_text(line)
            body.append("\n")
        return Panel(
            Group(body, Text(self.error, style=_WARN), Align.center(self.hint)),
            box=box.ROUNDED,
            border_style=_ACCENT,
            padding=(1, 3),
            title=Text(self.title, style=f"bold {_ACCENT}")
            if isinstance(self.title, str)
            else self.title,
            title_align="center",
        )


class InputModal:
    """A centered free-text field; enter saves, esc keeps the old value."""

    def __init__(
        self,
        title: str,
        value: str,
        *,
        width: int = 52,
        max_len: int = 48,
        masked: bool = False,
        placeholder: str = "",
        validate: Callable[[str], object] | None = None,
    ) -> None:
        self.title = title
        self.value = value
        self.cur = len(value)
        self.width = width
        self.max_len = max_len
        self.masked = masked
        self.revealed = False
        self.placeholder = placeholder
        self.validate = validate
        self.error = ""
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        if key == "enter":
            try:
                if self.validate is not None:
                    self.validate(self.value)
            except ValueError as exc:
                self.error = str(exc)
                return
            self.finish(self.value)
        elif key == "escape":
            self.finish(None)
        elif key == "tab" and not self.value and self.placeholder:
            self.value = self.placeholder
            self.cur = len(self.value)
            self.error = ""
        elif key == "tab" and self.masked:
            self.revealed = not self.revealed
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

    def render(self, width: int | None = None, height: int | None = None) -> Panel:
        width = width or self.width
        line = Text()
        line.append("❯ ", style=_ACCENT)
        displayed = (
            "•" * len(self.value) if self.masked and not self.revealed else self.value
        ) or self.placeholder
        style = "dim" if self.placeholder and not self.value else ""
        start = max(0, self.cur - (width - 12))
        line.append(displayed[start : self.cur], style=style)
        at = displayed[self.cur : self.cur + 1] or " "
        line.append(at, style=f"reverse {style}")
        line.append(displayed[self.cur + 1 : start + width - 10], style=style)
        hints = []
        if self.placeholder and not self.value:
            hints.append(("tab", "fill"))
        if self.masked:
            hints.append(("tab", "hide" if self.revealed else "show"))
        hint = _hints(*hints, ("enter", "done"), ("esc", "cancel"))
        return Panel(
            Group(line, Text(self.error, style=_WARN), Align.center(hint)),
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

    def render(self, width: int | None = None, height: int | None = None) -> Panel:
        width = width or self.width
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


class NoticeModal:
    """A dismissible message, shared by warnings and field help."""

    def __init__(
        self, message: str, *, title: str = "Warning", tone: str = _WARN
    ) -> None:
        self.message = message
        self.scroll = 0
        self.page_size = 1
        self.title = title
        self.tone = tone
        self.width = 68
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        if key in ("enter", "escape", "q"):
            self.finish(None)
        elif key in ("down", "j"):
            self.scroll += 1
        elif key in ("up", "k"):
            self.scroll = max(0, self.scroll - 1)

    def render(self, width: int | None = None, height: int | None = None) -> Panel:
        width = width or self.width
        lines = [
            line
            for paragraph in self.message.split("\n")
            for line in (textwrap.wrap(paragraph, width - 8) or [""])
        ]
        self.page_size = max(1, (height or 80) - 7)
        self.scroll = min(self.scroll, max(0, len(lines) - self.page_size))
        hints = [("enter", "close"), ("esc", "close")]
        if len(lines) > self.page_size:
            hints.append(("↑↓", "scroll"))
        return Panel(
            Group(
                Text("\n".join(lines[self.scroll : self.scroll + self.page_size])),
                Text(""),
                Align.center(_hints(*hints)),
            ),
            box=box.ROUNDED,
            border_style=self.tone,
            padding=(1, 3),
            title=Text(self.title, style=f"bold {self.tone}"),
            title_align="center",
        )


class CheckModal:
    """A checklist of models; esc goes back with what is ticked.

    Nothing here is a draft to cancel: ticks, fetched rows and the edits made
    one level down all stay, the way a pick in the agents list stays. One rule
    for every list in the menu -- esc is back -- so an edit kept with "back"
    cannot be lost by the next "back".
    """

    def __init__(
        self,
        items: list[_ModelPick],
        *,
        on_details: Callable[[_ModelPick], None],
        on_fetch: Callable[[], None],
        on_add: Callable[[], None],
    ) -> None:
        self.items = items
        self.cursor = 0
        self.on_details = on_details
        self.on_fetch = on_fetch
        self.on_add = on_add
        self.error = ""
        self.width = 76
        self.page_size = 10
        self.finish: Callable[[object], None] = lambda result: None

    def handle(self, key: str) -> None:
        self.error = ""
        if key in ("escape", "q"):
            self.finish([item for item in self.items if item.on])
        elif key == "tab":
            self.on_fetch()
        elif key == "a":
            self.on_add()
        elif self.items:
            if key in ("down", "j"):
                self.cursor = (self.cursor + 1) % len(self.items)
            elif key in ("up", "k"):
                self.cursor = (self.cursor - 1) % len(self.items)
            elif key == "home":
                self.cursor = 0
            elif key == "end":
                self.cursor = len(self.items) - 1
            elif key == "space":
                item = self.items[self.cursor]
                item.on = not item.on
            elif key == "enter":
                self.on_details(self.items[self.cursor])

    def render(self, width: int | None = None, height: int | None = None) -> Panel:
        width = width or self.width
        inner = width - 8
        body = Text()
        if not self.items:
            body.append("  No models yet.\n", style="dim")
        start = max(0, self.cursor - self.page_size + 1)
        for i in range(start, min(len(self.items), start + self.page_size)):
            item = self.items[i]
            current = i == self.cursor
            line = Text()
            line.append("❯ " if current else "  ", style=_ACCENT)
            line.append(
                "◉ " if item.on else "○ ",
                style=_ACCENT if item.on else f"dim {_ACCENT}",
            )
            line.append(item.model.label, style="bold" if current else "")
            if item.model.name:
                line.append(f" · {item.model.id}", style="dim")
            line.truncate(inner, overflow="ellipsis")
            body.append_text(line)
            body.append("\n")
        hints = _hints(
            ("tab", "fetch"),
            ("space", "toggle"),
            ("a", "add"),
            ("enter", "details"),
            ("esc", "back"),
        )
        return Panel(
            Group(body, Text(self.error, style=_WARN), Align.center(hints)),
            box=box.ROUNDED,
            border_style=_ACCENT,
            padding=(1, 3),
            title=Text("Models", style=f"bold {_ACCENT}"),
            title_align="center",
        )


Modal = PickModal | InputModal | ConfirmModal | CheckModal | NoticeModal


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
        self._initial = model.selection().payload()
        self.cursor = 0
        self.view = "select"  # select | busy | report | doctor
        self.stack: list[tuple[Modal, Callable[[object], None] | None]] = []
        self.report: patcher.PatchReport | None = None
        #: What the last apply could not do *after* the binary was written --
        #: remember the selection, save the key. Shown on the report, never in
        #: its place: the patch succeeded, and the report is the proof.
        self.report_note = ""
        self.doctor_result: DryRun | None = None
        self.busy_message = ""
        self.flash = ""
        self.exit_code = 0
        self.exit_message: str | None = None
        self._exit: int | None = None
        #: A worker's tagged result, read by the loop: (kind, payload, error).
        self._worker_result: tuple[str, Any, str | None] | None = None
        #: The custom-models row awaiting the submenu's result.
        self._custom_row: PatchRow | None = None
        self._frame = 0
        self._scroll = 0
        self._needs_paint = True
        self._last_size = (0, 0)
        color = self.console.color_system
        self._color_system = COLOR_SYSTEMS.get(color) if color else None
        self._clamp_cursor(0)
        if model.notice:
            self._push(NoticeModal(model.notice, title="Saved selection"), None)

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
        except Exception:  # noqa: BLE001, S110 - best effort; no termios, no freeze risk
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
                term.KEY_TAB: "tab",
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
            "\t": "tab",
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
        elif key == "s":
            self._start_apply()
        elif key == "d":
            self._start_doctor()
        elif key == "r":
            self._confirm_restore()

    def _activate(self, row: PatchRow) -> None:
        """Enter on a patch: toggle plain ones, configure configurable ones."""
        patch_id = row.patch.id
        text_keys = {
            "branding": "brand",
            "version-marker": "suffix",
            "org-label": "org",
        }
        if patch_id == "subagent-models":
            if not self.model.agent_rows:
                self.flash = "no agents discovered in this bundle"
                return
            self._open_agents_modal()
        elif patch_id in text_keys:
            self._open_text_modal(text_keys[patch_id])
        elif patch_id == "custom-models":
            self._open_custom_menu(row)  # submenu: pick models / set endpoint URL
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
                hint=_hints(("enter", "choose model"), ("esc", "back")),
            ),
            None,
        )

    def _open_model_modal(self, agent_row: AgentRow) -> None:
        custom_ids = [model.id for model in self.model.custom_models]
        items = [_KEEP, *self.model.models, *custom_ids]
        custom_set = set(custom_ids)

        def label(value: str, _selected: bool) -> Text:
            """The handle you pick, then a dim parenthetical explaining it.

            Every row is that one shape -- what `keep` would leave in place, what
            `inherit` resolves to, where a custom model came from -- so the branches
            below only choose the words, never how they are drawn.
            """
            head, note = value, ""
            if value == _KEEP:
                head, note = "keep default", agent_row.agent.effective_model
            elif value == INHERIT:
                note = "main model"
            elif value in custom_set:
                note = "custom"
            text = Text(head)
            if note:
                text.append(f" ({note})", style="dim")
            return text

        def picked(choice: object) -> None:
            if not isinstance(choice, str):
                return
            agent_row.choice = choice
            self.model.patch_rows["subagent-models"].on = bool(self.model.overridden())
            # A custom model only resolves once custom-models registers it, so the
            # pin selects that row too -- here, where the tick visibly moves,
            # rather than at save time behind a row still drawn as off.
            if choice in custom_set:
                self.model.patch_rows["custom-models"].on = True

        self._push(
            PickModal(
                f"Model for {agent_row.agent.name}",
                items,
                label,
                current=custom.aliases(self.model.custom_models).get(
                    agent_row.choice, agent_row.choice
                ),
                width=46,
            ),
            picked,
        )

    def _open_text_modal(self, key: str) -> None:
        row = self.model.text_rows[key]
        titles = {
            "brand": "Startup name",
            "suffix": "--version marker",
            "org": "Org/email label (empty hides it)",
        }

        def entered(value: object) -> None:
            # For the org label an emptied field is the point -- it means hide
            # the segment -- where an empty brand or marker means "never mind".
            if isinstance(value, str) and (value.strip() or key == "org"):
                row.value = value.strip()
                patch_id = {
                    "brand": "branding",
                    "suffix": "version-marker",
                    "org": "org-label",
                }[key]
                self.model.patch_rows[patch_id].on = (
                    key != "brand" or row.value != DEFAULT_BRAND
                )

        self._push(InputModal(titles[key], row.value), entered)

    # -- custom models: one configuration, populated manually or by discovery

    def _endpoint_key(self) -> str:
        return (
            self.model.pending_key
            if self.model.pending_key is not None
            else custom.read_key()
        )

    def _open_custom_menu(self, row: PatchRow) -> None:
        self._custom_row = row
        # Read once on the way in, not per repaint: the row only has to say
        # whether a key exists, and an edit this session overrides it anyway.
        try:
            saved_key: str | None = custom.read_key()
        except ValueError:
            saved_key = None

        def label(item: str, selected: bool) -> Text:
            key = (
                self.model.pending_key
                if self.model.pending_key is not None
                else saved_key
            )
            values = {
                "Endpoint": self.model.endpoint or "not set",
                "Key": "unreadable" if key is None else "set" if key else "none",
                "Models": f"{len(self.model.custom_models)} selected",
            }
            text = Text(f"{item:<10}", style="bold" if selected else "")
            text.append(values[item], style="dim not bold")
            return text

        def chosen(item: str) -> None:
            if item == "Endpoint":
                self._open_endpoint()
            elif item == "Key":
                self._open_key()
            elif item == "Models":
                self._open_custom_picker()
                # Fetch on the way in when there is somewhere to fetch from;
                # without an endpoint the list says so where `tab` would.
                if self.model.endpoint:
                    self._start_custom_discovery()
            elif item == "help":
                self._open_help(submenu)

        submenu = PickModal(
            "Custom models",
            ["Endpoint", "Key", "Models"],
            label,
            on_pick=chosen,
            width=68,
            shortcuts={"?": "help"},
            hint=_hints(("?", "help"), ("enter", "open"), ("esc", "back")),
        )
        self._push(submenu, None)

    @staticmethod
    def _open_help(modal: PickModal) -> None:
        """``?``: the README section in the browser -- or its address, shown.

        Only a browser that runs *beside* this terminal is asked: without a
        display, Python's fallback is a console browser opened over the menu.
        There, and wherever opening fails, the address is the help.

        The launcher inherits this terminal, and what it starts chatters on it
        later -- "Opening in existing browser session.", a GTK warning --
        under a frame that has already been repainted. So it is spawned with
        the terminal's own descriptors pointed at ``/dev/null`` for exactly that
        moment; a child keeps the descriptors it was given, and the menu gets
        its own back before it draws again.
        """
        import os
        import webbrowser

        opened = False
        if (
            sys.platform != "linux"
            or os.environ.get("DISPLAY")
            or os.environ.get("WAYLAND_DISPLAY")
        ):
            sys.stdout.flush()
            sys.stderr.flush()
            kept = [os.dup(fd) for fd in (1, 2)]
            try:
                with open(os.devnull, "wb") as quiet:
                    for fd in (1, 2):
                        os.dup2(quiet.fileno(), fd)
                    try:
                        opened = webbrowser.open(_CUSTOM_MODELS_HELP, new=2)
                    except Exception:  # noqa: BLE001 - whatever failed, the address still answers
                        opened = False
            finally:
                for fd, original in zip((1, 2), kept):
                    os.dup2(original, fd)
                    os.close(original)
        modal.error = "" if opened else f"read {_CUSTOM_MODELS_HELP}"

    def _open_endpoint(self) -> None:
        def entered(value: object) -> None:
            if isinstance(value, str):
                self.model.endpoint = custom.endpoint(value)

        self._push(
            InputModal(
                "Endpoint · base before /v1/messages",
                self.model.endpoint,
                width=68,
                max_len=2048,
                placeholder="http://127.0.0.1:8317",
                validate=custom.endpoint,
            ),
            entered,
        )

    def _open_key(self) -> None:
        """One masked field, like the org label: emptied, it means keyless."""
        try:
            key = self._endpoint_key()
        except ValueError as exc:
            self._push(NoticeModal(str(exc)), None)
            return

        def entered(value: object) -> None:
            if isinstance(value, str):
                self.model.pending_key = custom.key_value(value)

        self._push(
            InputModal(
                "Key · empty = keyless",
                key,
                width=68,
                max_len=4096,
                masked=True,
                validate=custom.key_value,
            ),
            entered,
        )

    def _start_custom_discovery(self) -> None:
        self._start_worker(
            "custom-discover",
            "Fetching endpoint models …",
            lambda: custom.discover(self.model.endpoint, self._endpoint_key()),
        )

    def _open_custom_picker(self) -> None:
        def fetch() -> None:
            if self.model.endpoint:
                self._start_custom_discovery()
            else:
                picker.error = (
                    "set an endpoint first; models can still be added by hand"
                )

        picker = CheckModal(
            [_ModelPick(model, True) for model in self.model.custom_models],
            on_details=lambda pick: self._custom_details(picker, pick),
            on_fetch=fetch,
            on_add=lambda: self._custom_add(picker),
        )
        self._push(picker, self._custom_picked)

    def _custom_check(
        self, picker: CheckModal, pick: _ModelPick | None, model: CustomModel
    ) -> None:
        """Refuse a handle the list or the binary already answers to."""
        if any(item is not pick and item.model.id == model.id for item in picker.items):
            raise ValueError(f"Model {model.id!r} is already in the list")
        others = [item.model for item in picker.items if item.on and item is not pick]
        validate_models(self.model.pristine.source, [*others, model])

    def _custom_add(self, picker: CheckModal) -> None:
        """``a``: ask for the id, add the model, then open it like any other.

        The id is the one field a model cannot exist without, so it is the one
        asked for up front; everything else is edited in the same form an
        existing model gets. There is no separate "new model" form to cancel
        out of -- enter on the id adds the row, esc on it adds nothing.
        """

        def valid(raw: str) -> None:
            self._custom_check(picker, None, CustomModel(custom.model_id(raw.strip())))

        def entered(raw: object) -> None:
            if isinstance(raw, str):
                pick = _ModelPick(CustomModel(custom.model_id(raw.strip())), True)
                picker.items.append(pick)
                picker.cursor = len(picker.items) - 1
                self._custom_details(picker, pick)

        self._push(
            InputModal("Model ID", "", width=68, max_len=200, validate=valid), entered
        )

    def _refresh_custom_picker(self, offered: list[CustomModel]) -> None:
        picker = next(
            modal for modal, _ in reversed(self.stack) if isinstance(modal, CheckModal)
        )
        current = picker.items[picker.cursor].model.id if picker.items else ""
        claimed = claimed_model_names(self.model.pristine.source)
        reported = {model.id: model for model in offered if model.id not in claimed}
        for pick in picker.items:
            if pick.model.id in reported:
                pick.model = replace(
                    pick.model,
                    context_options=reported[pick.model.id].context_options
                    or pick.model.context_options,
                )
        listed = {pick.model.id for pick in picker.items}
        picker.items += [
            _ModelPick(model)
            for identity, model in reported.items()
            if identity not in listed
        ]
        picker.cursor = next(
            (i for i, pick in enumerate(picker.items) if pick.model.id == current), 0
        )

    def _custom_details(self, picker: CheckModal, pick: _ModelPick) -> None:
        """A model's editable fields; each edit lands on the row as it is made."""

        def label(item: str, selected: bool) -> Text:
            values = {
                "Name": pick.model.label or "none",
                "Alias": pick.model.alias or "none",
                "Context": f"{pick.model.context:,} tokens"
                if pick.model.context
                else "unknown",
                "Efforts": ", ".join(pick.model.efforts) or "off",
            }
            text = Text()
            text.append(f"{item:<10}", style="bold" if selected else "dim")
            empty = item == "Alias" and not pick.model.alias
            text.append(
                values[item],
                style="dim italic not bold" if empty else f"{_VALUE} not bold",
            )
            return text

        def edit(item: str) -> None:
            form.error = ""
            if item == "Context":
                self._custom_context(pick)
                return
            if item == "Efforts":
                self._custom_efforts(pick)
                return
            model = pick.model
            value = {"Name": model.name, "Alias": model.alias}[item]

            def changed(raw: str) -> CustomModel:
                if item == "Alias":
                    return replace(pick.model, alias=custom.model_alias(raw))
                return replace(pick.model, name=raw.strip())

            def entered(raw: object) -> None:
                if isinstance(raw, str):
                    pick.model = changed(raw)
                    pick.on = True
                    form.title = pick.model.label

            title = {
                "Alias": "/model shortcut · empty = none",
                "Name": "Display name",
            }[item]
            self._push(
                InputModal(
                    title,
                    value,
                    width=68,
                    max_len=200,
                    validate=lambda raw: self._custom_check(picker, pick, changed(raw)),
                ),
                entered,
            )

        form = PickModal(
            pick.model.label,
            ["Name", "Alias", "Context", "Efforts"],
            label,
            on_pick=edit,
            width=68,
            hint=_hints(("enter", "edit"), ("esc", "back")),
            description=Text(f"ID  {pick.model.id}", style="dim"),
        )
        self._push(form, None)

    def _custom_efforts(self, pick: _ModelPick) -> None:
        """The ladder as the binary can bake it, shaped like the context picker.

        Four shapes exist: the ladder cut at each top the registry names
        (`low,medium,high`, `+xhigh`, `+max`) and *off*. A free field offers
        more precision than the binary can bake -- `low,high` bakes exactly as
        `low,medium,high` -- and an empty one is *off*, never a third "unknown"
        state: on the API path an undeclared model is offered every level, the
        opposite of not knowing and the one wrong *yes* this patch could bake.
        Off is where a model that reports nothing starts. A discovered odd list
        still shows, under `c`, the way an odd context window does.
        """
        ladder = [
            ",".join(custom.EFFORT_LADDER[: top + 1])
            for top in range(2, len(custom.EFFORT_LADDER))
        ]
        choices = [*ladder, "off"]
        current = ",".join(pick.model.efforts) or "off"

        def enter_custom() -> None:
            def entered(value: object) -> None:
                if isinstance(value, str):
                    pick.model = replace(
                        pick.model, efforts=custom.effort_levels(value)
                    )
                    pick.on = True

            self._push(
                InputModal(
                    "Efforts · comma-separated · empty = off",
                    "" if current == "off" else current,
                    width=68,
                    max_len=200,
                    validate=custom.effort_levels,
                ),
                entered,
            )

        def label(value: str, selected: bool) -> Text:
            emphasis = "bold" if selected else "not bold"
            if value == "custom":
                if current not in choices:
                    text = Text("Custom", style=emphasis)
                    text.append(
                        f" {current.replace(',', ', ')}", style=f"dim {emphasis}"
                    )
                    return text
                return Text("Custom", style="dim italic")
            if value == "off":
                text = Text("Off", style=emphasis)
                text.append("  no effort control", style=f"dim {emphasis}")
                return text
            return Text(value.replace(",", " · "), style=emphasis)

        def chosen(value: object) -> None:
            if value == "custom":
                enter_custom()
            elif isinstance(value, str):
                pick.model = replace(pick.model, efforts=custom.effort_levels(value))
                pick.on = True

        self._push(
            PickModal(
                "Efforts",
                [*choices, "custom"],
                label,
                current=current if current in choices else "custom",
                shortcuts={"c": "custom"},
                width=60,
                hint=_hints(("c", "custom"), ("enter", "select"), ("esc", "cancel")),
            ),
            chosen,
        )

    def _custom_context(self, pick: _ModelPick) -> None:
        def enter_custom() -> None:
            def entered(value: object) -> None:
                if isinstance(value, str):
                    pick.model = replace(
                        pick.model, context=custom.context_tokens(value)
                    )
                    pick.on = True

            self._push(
                InputModal(
                    "Context tokens · 0 = unknown",
                    str(pick.model.context),
                    width=68,
                    max_len=24,
                    validate=custom.context_tokens,
                ),
                entered,
            )

        windows = pick.model.context_options
        if len(windows) < 2:
            enter_custom()
            return

        def label(value: str, selected: bool) -> Text:
            emphasis = "bold" if selected else "not bold"
            if value == "custom":
                if pick.model.context and pick.model.context not in windows:
                    text = Text("Custom", style=emphasis)
                    text.append(f" {pick.model.context:,}", style=f"dim {emphasis}")
                    return text
                return Text("Custom", style="dim italic")
            return Text(f"{int(value):,}", style=emphasis)

        def chosen(value: object) -> None:
            if value == "custom":
                enter_custom()
            elif isinstance(value, str):
                pick.model = replace(pick.model, context=int(value))
                pick.on = True

        self._push(
            PickModal(
                "Context window",
                [*(str(value) for value in windows), "custom"],
                label,
                current=str(pick.model.context)
                if pick.model.context in windows
                else "custom",
                shortcuts={"c": "custom"},
                width=60,
                hint=_hints(("c", "custom"), ("enter", "select"), ("esc", "cancel")),
            ),
            chosen,
        )

    def _custom_picked(self, result: object) -> None:
        if not isinstance(result, list):
            return
        previous = {model.id for model in self.model.custom_models}
        self.model.custom_models = custom.models_from(
            custom.model_values([pick.model for pick in result])
        )
        chosen = {model.id for model in self.model.custom_models}
        offered = {
            *self.model.models,
            *custom.model_names(self.model.custom_models),
        }
        reverted = []
        for row in self.model.agent_rows:
            if row.choice != _KEEP and row.choice not in offered:
                row.choice = _KEEP
                reverted.append(row.agent.name)
        if self._custom_row is not None:
            self._custom_row.on = bool(chosen) and (
                self._custom_row.on or bool(chosen - previous)
            )
        if reverted:
            self.flash = (
                "reset " + ", ".join(reverted) + " to keep (model or alias removed)"
            )

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
                ConfirmModal("Discard unsaved changes?", "discard", tone=_WARN),
                answered,
            )
            return
        self._exit = self.exit_code if self.view in ("report", "doctor") else 0

    def _unsaved(self) -> bool:
        if (
            self.model.pending_key is not None
            and self.model.pending_key != custom.read_key()
        ):
            return True
        return self.model.selection().payload() != self._initial

    # ---------------------------------------------------- actions

    def _start_apply(self) -> None:
        # A row that is on with nothing chosen is dropped by `Selection.active`,
        # like subagent-models. What cannot be dropped is a chosen set with no
        # endpoint, or a saved id that this (newer) binary now claims for itself
        # -- the patch would refuse either, so it is said here, in the submenu
        # that fixes it, rather than as a broken patch on the report.
        row = self.model.patch_rows["custom-models"]
        if row.on and self.model.custom_models:
            try:
                validate_models(self.model.pristine.source, self.model.custom_models)
                problem = (
                    "" if self.model.endpoint else "Custom models needs an endpoint"
                )
            except ValueError as exc:
                problem = str(exc)
            if problem:
                self.flash = problem
                self._open_custom_menu(row)
                return
        selection = self.model.selection()
        count = len(selection.active().patches)
        if not count:
            self._confirm_restore()
            return

        def confirmed(save: object) -> None:
            if not save:
                return
            self._start_worker(
                "apply",
                f"Patching Claude {self.model.install.version or '?'} …",
                lambda: self._apply(selection),
            )

        self._push(
            ConfirmModal(
                f"Apply {count} patch{'es' if count != 1 else ''} to Claude {self.model.install.version or '?'}?",
                "apply",
            ),
            confirmed,
        )

    def _apply(
        self, selection: cache.Selection
    ) -> tuple[patcher.PatchReport, Status | None, str]:
        active = selection.active()
        report = patcher.patch_installation(
            self.model.install,
            active.patches,
            active.options,
            bundle=self.model.pristine,
        )
        status, note = None, ""
        if report.output is not None:
            # Remembered once it is really in the binary, never before -- the
            # same rule `apply` follows. Saving on the way in would have a run
            # that then failed still rewrite what the next `apply` bakes.
            # And a memory that cannot be written is a note on the report, never
            # the result in its place: the binary is patched either way, and
            # the header keeps saying "unsaved", which is now simply true.
            try:
                cache.save(selection)
                self._initial = selection.payload()
                if self.model.pending_key is not None:
                    custom.save_key(self.model.pending_key)
                    self.model.pending_key = None
            except OSError as exc:
                note = f"selection not remembered: {exc}"
            # The binary just changed; recompute the header state off-loop. A
            # failure here costs only the refreshed header, not the apply that
            # already succeeded, so it stays a local miss rather than a result.
            try:
                from . import doctor

                status = doctor.status(container.read(str(self.model.install.binary)))
            except (BunError, OSError):
                status = None
        return report, status, note

    def _start_doctor(self) -> None:
        from . import doctor

        # Matcher health needs a *clean* bundle. When the installed binary is
        # patched and no backup exists, `pristine` is that patched bundle -- our
        # own edits removed the anchors the matchers look for, so every one would
        # read broken on a healthy build, under a message promising a clean one.
        # The CLI refuses this (cli._doctor_target); so does the menu.
        if patcher.is_patched(self.model.pristine.source):
            self.flash = (
                "installed binary is patched and no clean backup exists — "
                "restore first to check matcher health"
            )
            return
        self._start_worker(
            "doctor",
            "Checking every patch against a clean bundle …",
            lambda: doctor.dryrun(self.model.pristine),
        )

    def _start_restore(self) -> None:
        self._start_worker(
            "restore",
            "Restoring the original binary …",
            lambda: patcher.restore(self.model.install),
        )

    def _busy(self, message: str) -> None:
        self.view = "busy"
        self.busy_message = message
        self._needs_paint = True

    def _start_worker(self, kind: str, message: str, work: Callable[[], Any]) -> None:
        """Show ``message``, run ``work`` off-loop, and post its result exactly once.

        Failing is a result too. Every worker posts its outcome so the busy
        view always returns control, including when discovery fails.
        """
        self._busy(message)

        def run() -> None:
            try:
                self._worker_result = (kind, work(), None)
            except Exception as exc:  # noqa: BLE001 - the worker's failure IS the result
                self._worker_result = (kind, None, str(exc) or exc.__class__.__name__)

        threading.Thread(target=run, daemon=True).start()

    def _poll_worker(self) -> None:
        result = self._worker_result
        if result is None:
            return
        self._worker_result = None
        self._needs_paint = True
        kind, payload, error = result
        if error is not None:
            self.view = "select"
            self.flash = error
            if kind in ("apply", "doctor"):
                self.exit_code = 1
            if kind == "custom-discover":
                self.flash = ""
                self._push(
                    NoticeModal(
                        "Could not fetch models. Saved models are still editable.\n\n"
                        + error
                    ),
                    None,
                )
            return

        if kind == "apply":
            report, status, self.report_note = payload
            self.report = report
            self.exit_code = 0 if report.ok else 1
            if status is not None:
                self.model.status = status
            self.view = "report"
        elif kind == "doctor":
            self.doctor_result = payload
            # `clean`, not `broken`: a partially-drifted patch or a bundle that
            # will not parse is not green, and reading `broken` alone here would
            # exit 0 where the CLI exits 1 on the same build.
            self.exit_code = 0 if payload.clean else 1
            self.view = "doctor"
        elif kind == "restore":
            self.exit_message = "Restored the original binary. Restart Claude Code."
            self._exit = 0
        elif kind == "custom-discover":
            self.view = "select"
            self._refresh_custom_picker(payload)

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
        if isinstance(modal, (PickModal, CheckModal)):
            modal.page_size = (
                min(10, len(modal.items))
                if isinstance(modal, CheckModal)
                else len(modal.items)
            )

        def render() -> list[list[Segment]]:
            panel = modal.render(modal_width, height)
            if self.view == "busy":
                panel.title = Text(self.busy_message, style=f"bold {_ACCENT}")
            return self.console.render_lines(panel, options, pad=True)

        modal_lines = render()
        while (
            isinstance(modal, (PickModal, CheckModal))
            and len(modal_lines) > height
            and modal.page_size > 1
        ):
            modal.page_size = max(1, modal.page_size - (len(modal_lines) - height))
            modal_lines = render()
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
        title = Text("patch-cc", style=f"bold {_ACCENT}")
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
            applied = len(model.applied_ids())
            line.append("patched", style="green")
            if applied:
                line.append(f" ({applied})", style="dim")
        else:
            line.append("not patched", style=_WARN)
        if self.view == "select":
            note = (
                "unsaved"
                if self._unsaved()
                else "pending apply"
                if cache.pending(self.model.selection(), model.status.manifest)
                else ""
            )
            if note:
                line.append("  ·  ", style="dim")
                line.append(note, style=_WARN)
        return line

    def _foot(self, panel_width: int) -> list[Text]:
        lines = [Text("─" * panel_width, style=_RULE)]
        if self.view == "select":
            # Wrapped, not truncated. The longest thing that lands here is the
            # "already patched and no pristine backup exists" refusal, whose
            # whole value is the sentence naming the way out -- which one centred
            # line cut at the panel edge would throw away.
            lines += _center_block(textwrap.wrap(self.flash, panel_width), panel_width)
            rows = self.model.rows()
            row = rows[self.cursor] if self.cursor < len(rows) else None
            if isinstance(row, PatchRow) and row.patch.id in _CONFIGURABLE:
                context = _hints(("space", "toggle"), ("enter", "configure"))
            else:
                context = _hints(("enter", "toggle"))
            lines.append(_center(context, panel_width))
            lines.append(
                _center(
                    _hints(
                        ("s", "apply"), ("d", "doctor"), ("r", "restore"), ("q", "quit")
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
        if row.patch.id == "org-label":
            value = model.text_rows["org"].value
            return Text(value, style=_VALUE) if value else Text("hidden", style="dim")
        if row.patch.id == "custom-models":
            count = len(model.custom_models)
            if not count:
                return Text("none chosen", style="dim")
            note = Text(f"{count} model{'s' if count != 1 else ''}", style=_VALUE)
            note.append(f"  ·  {model.endpoint or 'no endpoint'}", style="dim")
            return note
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
        options = self.model.selection().active().options
        for patch, outcome in report.results:
            mark, style = MARKS[outcome.health]
            line = Text()
            line.append(f"  {mark} ", style=style)
            line.append(f"{patch.title:<32}")
            line.append(f"{outcome.applied or '':>3}", style="dim")
            if value := applied_value(patch, outcome, options):
                line.append(f"  → {value}", style="dim")
            lines.append(line)
            lines += _findings(outcome)

        lines.append(Text(""))
        if report.regressions:
            # The crosses above say a patch failed; this says what became of it.
            lines.append(
                Text(
                    f"  ! Left out of the binary: "
                    f"{', '.join(p.id for p in report.regressions)}",
                    style=_WARN,
                )
            )
        if report.output is None:
            line = Text()
            line.append("  ✗ ", style="red")
            line.append("No patch changed anything; binary left untouched.")
            lines.append(line)
        else:
            grown = (report.patched_size - report.original_size) / 1e6
            line = Text()
            line.append("  ✓ ", style="green")
            line.append(f"Applied to {report.output.name}", style="bold")
            line.append(
                f"  ·  {report.patched_size / 1e6:.0f} MB ({grown:+.0f} MB)",
                style="dim",
            )
            lines.append(line)
            lines.append(Text("    Restart Claude Code to see it.", style="dim"))
            if "custom-models" in report.landed_ids:
                # The binary now routes custom models to this endpoint; the run that
                # made that true is the only one that knows to say so.
                style, note = endpoint_note(options.endpoint)
                lines.append(Text(f"    endpoint  {note}", style=style))
            # The replay hint is a promise the cache keeps; when it could not
            # be written, the line that says so takes the promise's place.
            if self.report_note:
                lines.append(Text(f"    ! {self.report_note}", style=_WARN))
            else:
                lines.append(
                    Text(
                        "    After a Claude update, this is all you need:  patch-cc apply",
                        style="dim",
                    )
                )
        return lines, None

    def _body_doctor(self, panel_width: int) -> tuple[list[Text], int | None]:
        lines: list[Text] = []
        result = self.doctor_result
        if result is None:
            return lines, None
        for patch, outcome in result.results:
            mark, style = MARKS[outcome.health]
            line = Text()
            line.append(f"  {mark} ", style=style)
            line.append(f"{patch.id:<22}")
            line.append(
                f"cand={outcome.candidates:<3} applied={outcome.applied}", style="dim"
            )
            lines.append(line)
            lines += _findings(outcome)
        for patch, _why in result.absent:
            # Not a verdict: the build has no surface for this patch, so it was
            # not run. The sentence itself prints with the verdicts below.
            line = Text()
            line.append("  - ", style="dim")
            line.append(f"{patch.id:<22}", style="dim")
            line.append("not on this build", style="dim")
            lines.append(line)
        lines.append(Text(""))
        lines.append(
            Text(f"  agents  {', '.join(a.name for a in result.agents)}", style="dim")
        )
        lines.append(Text(f"  models  {', '.join(result.models)}", style="dim"))
        # The same verdict the CLI reaches -- the parse defect, the drift note,
        # the all-clear -- drawn from the one home so the two surfaces agree.
        summary = verdicts(result)
        if summary:
            lines.append(Text(""))
            lines += [Text(f"  {text}", style=style) for style, text in summary]
        return lines, None


def _findings(outcome) -> list[Text]:
    """The same detail lines the CLI prints, drawn as panel rows."""
    return [Text(f"      {text}", style=style) for style, text in findings(outcome)]


def _center(text: Text, width: int) -> Text:
    pad = max(0, (width - text.cell_len) // 2)
    line = Text(" " * pad)
    line.append_text(text)
    return line


def _center_block(lines: list[str], width: int, style: str = _WARN) -> list[Text]:
    """Centre wrapped text as one block, sharing a left edge.

    Centring each line on its own makes a broken path zig-zag down the panel;
    one edge is what lets several lines still read as a single sentence.
    """
    pad = " " * max(0, (width - max((len(line) for line in lines), default=0)) // 2)
    return [Text(pad + line, style=style) for line in lines]


# ----------------------------------------------------------------- entry


def run_menu() -> int:
    if not (sys.stdout.isatty() and sys.stdin.isatty()):
        err("The interactive menu needs a terminal.")
        console.print(
            "  [dim]patch-cc apply bakes the saved selection without one[/dim]"
        )
        return 2

    install = locate.find()
    if install is None:
        err("No Claude Code native install found.")
        console.print(
            "  Install it with: [cyan]curl -fsSL https://claude.ai/install.sh | bash[/cyan]"
        )
        return 1

    try:
        installed, pristine = patcher.read_installation(install)
    except BunError as exc:
        err(str(exc))
        return 1

    from . import doctor

    status = doctor.status(installed)

    app = MenuApp(MenuModel.build(install, status, pristine))
    code = app.run()
    if app.exit_message:
        console.print(app.exit_message)
    return code
