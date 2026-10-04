"""The bundle as a language: locate by authored name, edit by grammar node.

Every matcher rule this project learned the hard way was a rule about
*approximating grammar with a regex* -- how to delimit an object literal without
a tokeniser, how to allow both `if(x)return null;` and `if(x){return null}`, how
to skip a call expression whose callee is minifier noise, how to match a set of
properties that upstream reorders at will. The rules were right; the medium
could not express them. A regex has to describe the syntax that merely *lies
between* its anchors, and that syntax is upstream's busiest surface.

So the bundle is parsed, and the parse is what locates. Two things stay durable
across builds: the **names upstream's authors wrote** (property names, string
literals, `case` labels, the API's own vocabulary) and the **shape of the
grammar**. Everything a minifier regenerates -- local identifiers, statement
order, comma-fusion versus separate statements, braces around a single
statement, whether a helper was extracted -- lives strictly between those two,
and a tree makes it invisible rather than obligatory.

The idiom is one line long:

    for node in source.find("latestThinkingSummary"):

then climb or descend to the node the edit belongs to, and replace *that node*.
Splices land on node boundaries, so the off-by-one that produced
``agentDefinitions:streamingThinking:X,ae`` -- counted as applied, verified
clean, and unable to start -- is not a bug that can be written here.

The tree is kept live. An edit costs one incremental reparse (~75 ms on a 25 MB
bundle, against ~3 s for a full one, and measured to produce a structurally
identical tree), so location is effectively free after the first parse, and the
syntax gate stops being a separate pass over the bundle: a `Source` that carries
a defect *is* the failure, checked wherever one is built.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tree_sitter import Node, Tree

_T = TypeVar("_T")

#: How much of the bundle to quote either side of a defect. Enough to see the
#: splice that caused it; a minified bundle has no line breaks to lean on.
_CONTEXT = 90

_NEWLINE = re.compile(rb"\n")


class SyntaxGateError(RuntimeError):
    """A rewrite left the bundle unparseable. Nothing has been written."""


def _parser():
    """The JavaScript parser, or a failure that names the missing package.

    Imported here rather than at module scope so the import error surfaces
    where the message can say what the parser is *for*, instead of as an
    unrelated traceback at CLI start-up.
    """
    try:
        import tree_sitter_javascript
        from tree_sitter import Language, Parser
    except ImportError as exc:  # pragma: no cover - install-shape problem
        raise SyntaxGateError(
            "the JavaScript parser is unavailable, so the bundle can neither be "
            "located in nor checked before writing; reinstall patch-cc with its "
            f"dependencies ({exc})"
        ) from exc
    return Parser(Language(tree_sitter_javascript.language()))


# --------------------------------------------------------------------- edits


@dataclass(frozen=True, slots=True)
class Edit:
    """One replacement of a half-open byte range in one module.

    Always built from a node, so a rewrite cannot land between grammar nodes.
    Insertions are the empty range at a node's edge -- the same operation, not a
    second kind of one, which is what lets :meth:`Source.apply` order a mixed
    batch by offset alone.

    The bundle is many modules now, each its own parse, and ``start``/``end`` are
    offsets *within one of them*. ``anchor`` is a node from the module the edit
    belongs to, so :meth:`Source.apply` can route the edit to that module and
    nowhere else -- the node carries its module the way an offset cannot. Every
    edit has one: it is either the node being rewritten, or (for the two bare
    offsets below) a node standing in the same module as the point.
    """

    start: int
    end: int
    text: bytes
    anchor: Node

    @classmethod
    def replace(cls, node: Node, text: bytes | str) -> Edit:
        return cls(node.start_byte, node.end_byte, _bytes(text), node)

    @classmethod
    def before(cls, node: Node, text: bytes | str) -> Edit:
        return cls(node.start_byte, node.start_byte, _bytes(text), node)

    @classmethod
    def after(cls, node: Node, text: bytes | str) -> Edit:
        return cls(node.end_byte, node.end_byte, _bytes(text), node)

    @classmethod
    def at(cls, offset: int, text: bytes | str, *, within: Node) -> Edit:
        """An insertion at a bare offset, for a point no node's edge names.

        There are two, and they are the whole licence for this constructor: a
        `switch` arm's dispatch point when the label chain carries no body of
        its own (:func:`dispatch` computes it), and the end of a module (the
        manifest append). Anything else has a node, and a node is what an edit
        should be built from -- including "the start of this function's body",
        which is the *first statement*, not one byte past a brace, and "just
        after this literal", which is its fragment's own edge (:meth:`after`).

        ``within`` is a node in the module the offset addresses -- the ``case``
        whose dispatch point this is, or the module the manifest lands in -- so
        the edit still routes to its module without being built from the node at
        the point itself.
        """
        return cls(offset, offset, _bytes(text), within)


def _bytes(text: bytes | str) -> bytes:
    return text if isinstance(text, bytes) else text.encode("utf8")


def _advance(point: tuple[int, int], text: bytes) -> tuple[int, int]:
    """Where ``text`` ends, inserted at ``point`` -- ``Tree.edit``'s new end.

    A row per newline it carries, and a column that continues the old one only
    while it carries none. Nearly every splice here is one line's worth of text
    into a bundle that is itself one enormous line -- but the manifest ends the
    file with real newlines, and a position that is right by luck is not a
    position. Passing the *start* point as the end would be inert only for as
    long as that stays true.
    """
    row, column = point
    lines = text.split(b"\n")
    return (row + len(lines) - 1, len(lines[-1]) + (column if len(lines) == 1 else 0))


# -------------------------------------------------------------------- source


@dataclass(frozen=True, slots=True)
class Defect:
    """Where the tree stopped making sense."""

    offset: int
    kind: str
    missing: bool
    context: str

    def __str__(self) -> str:
        what = f"missing {self.kind}" if self.missing else self.kind
        return f"{what} at byte {self.offset:,}\n    ...{self.context}..."


#: The module-linkage *clauses* the syntax gate reads *past*. tree-sitter's
#: JavaScript grammar does not model a reserved word as an import/export alias
#: (``export{x as if}``, ``import{if as a}`` -- legal ES2015, which the
#: code-split minifier emits when its two-letter alias generator lands on ``if``,
#: ``in`` or ``do``), so it plants a localized ERROR inside the specifier list.
#: patch-cc never locates in or edits module linkage -- every anchor is
#: executable code (a property, a `case` label, a call, a string *in code*) -- so
#: an error confined to a specifier list cannot touch a matcher or a splice.
#:
#: Only the *clause* nodes are here, never ``import_statement``/
#: ``export_statement``: those enclose executable code too -- ``export default
#: <expr>``, ``export const x = <code>`` -- and a splice that broke *that* must
#: still be caught. A specifier list holds nothing but names, so an error under
#: one of these is always the grammar's alias gap and never our damage; on every
#: build in the corpus the real errors sit directly under ``export_clause`` or
#: ``named_imports`` (docs/PLAYBOOK.md).
_LINKAGE = (
    "import_clause",
    "export_clause",
    "named_imports",
    "import_specifier",
    "export_specifier",
)


class _Module:
    """One module's bytes and its parse, kept together and kept in step.

    A build ships one such module before 2.1.242 (the whole app) and well over a
    thousand after it (the app dealt across ``chunk-*.js`` files); :class:`Source` spans
    them, and this is one. Immutable, and enforced rather than assumed:
    :meth:`apply` returns a new `_Module` and never touches this one's bytes or
    its tree, so a patch that raises half-way discards its own partial rewrites
    simply by not returning. The fixpoint leans on it, running every pass from
    the same pristine modules.

    Bytes, not text. tree-sitter indexes bytes, the container hands bytes over
    and takes bytes back, so decoding in the middle only bought a second unit of
    offset for a splice to be wrong in.

    The parse is lazy, and that is what keeps it from being a tax: a name is
    found, an anchor counted and the manifest read by byte scan, and a module a
    scan never hits is never parsed. The menu's first screen and `apply`'s
    seed ask only scans and never pay for grammar; an edit buys it, for the
    handful of modules an anchor actually lands in.
    """

    __slots__ = ("_lines", "_tree", "data", "dirty", "index")

    def __init__(
        self, data: bytes, index: int, tree: Tree | None = None, *, dirty: bool = False
    ) -> None:
        self.data = data
        #: The blob module-table index this came from, so the container can
        #: splice the edited bytes back into the right record.
        self.index = index
        #: Whether these bytes differ from the pristine module -- what the final
        #: from-scratch gate and the bytecode drop key off.
        self.dirty = dirty
        self._tree = tree
        self._lines: tuple[int, ...] | None = None

    @property
    def parsed(self) -> bool:
        return self._tree is not None

    @property
    def tree(self) -> Tree:
        if self._tree is None:
            self._tree = _parser().parse(self.data)
        return self._tree

    @property
    def root(self) -> Node:
        return self.tree.root_node

    def find(self, needle: bytes) -> Iterator[Node]:
        # Byte-scan first: a module the name is not in is never parsed.
        if needle not in self.data:
            return
        yield from (
            node
            for node in self._spanning(needle)
            if node.end_byte - node.start_byte == len(needle)
        )

    def literals(self, needle: bytes) -> Iterator[Node]:
        if needle not in self.data:
            return
        seen: set[int] = set()
        for node in self._spanning(needle):
            literal = up(node, "string", "template_string")
            if literal is not None and literal.start_byte not in seen:
                seen.add(literal.start_byte)
                yield literal

    def _spanning(self, needle: bytes) -> Iterator[Node]:
        root, at = self.root, self.data.find(needle)
        while at != -1:
            yield cast("Node", root.descendant_for_byte_range(at, at + len(needle)))
            at = self.data.find(needle, at + 1)

    def apply(self, edits: list[Edit]) -> _Module:
        """Splice a batch into this module and reparse, or raise on rubble.

        Applied high offset first so earlier edits keep the offsets every node
        in the batch was found at -- which is why a patch collects its edits and
        commits them once, rather than rewriting under its own feet.

        That ordering preserves *disjoint* edits and nothing else, so the range
        check below is the invariant rather than a bounds test: each edit has to
        lie in the bytes no earlier one has already rewritten. Two edits over one
        node splice each other's output -- replacing ``x`` twice in ``let x=1;``
        yields ``let abcdefbcdef=1;`` -- and that is the one corruption the gate
        cannot see, because it parses. A batch is a patch's own doing, so the
        overlap is a defect here rather than a shape upstream can ship: nothing
        in the corpus emits one, and a matcher that starts to (an `if` and the
        `if` nested inside it, both read as the same guard) is told so instead of
        writing the difference into a binary.

        The gate is here rather than at the end of the run because here it can
        say *which* rewrite produced rubble. `Patch.run` turns that into one
        broken patch dropped from the set, instead of a whole apply aborted with
        the culprit unnamed.

        The tree is copied before it is edited, which is what makes the
        immutability above true rather than merely intended. ``Tree.edit``
        mutates in place, so editing our own would leave the input holding a tree
        that no longer describes its bytes -- and the point is that the input
        survives a rewrite that fails. It would read as someone else's failure:
        the fixpoint re-runs every patch from the same source, so one drifted
        matcher would poison the second pass and every healthy patch report
        *its* anchors missing.
        """
        batch = sorted(edits, key=lambda e: (e.start, e.end), reverse=True)
        data, tree = self.data, self.tree.copy()
        untouched = len(data)
        for edit in batch:
            if not 0 <= edit.start <= edit.end <= untouched:
                raise SyntaxGateError(
                    f"edit [{edit.start}:{edit.end}] is not inside the "
                    f"{untouched:,} bytes this batch has not already rewritten"
                )
            untouched = edit.start
            data = data[: edit.start] + edit.text + data[edit.end :]
            tree.edit(
                start_byte=edit.start,
                old_end_byte=edit.end,
                new_end_byte=edit.start + len(edit.text),
                start_point=self._point(edit.start),
                old_end_point=self._point(edit.end),
                new_end_point=_advance(self._point(edit.start), edit.text),
            )
        result = _Module(data, self.index, _parser().parse(data, tree), dirty=True)
        found = result.defect()
        # Only a defect this rewrite *introduced* is this rewrite's to answer
        # for. A module that already does not parse (in code, not the tolerated
        # linkage) fails every splice made to it, so blaming the splice would
        # dress "this build is unreadable" as "this patch is broken" -- the one
        # collapse the report exists to prevent. That is a fact about the build,
        # asked of the pristine module here and reported as itself.
        if found is not None and self.defect() is None:
            raise SyntaxGateError(f"this rewrite left the bundle unparseable: {found}")
        return result

    def _point(self, offset: int) -> tuple[int, int]:
        """Byte offset as (row, column), which ``Tree.edit`` shifts positions by.

        Read against *this* module's bytes, which stays correct as a batch is
        spliced because the batch runs high offset first: every offset still to
        be edited sits in the part no earlier edit has touched.
        """
        from bisect import bisect_right

        if self._lines is None:
            self._lines = (0, *(m.end() for m in _NEWLINE.finditer(self.data)))
        row = bisect_right(self._lines, offset) - 1
        return (row, offset - self._lines[row])

    def defect(self) -> Defect | None:
        """The first place this module's *code* stops parsing, or ``None``.

        Reads past module linkage (`_LINKAGE`): the first ERROR or MISSING node
        not confined to an import/export clause. The gate reads executable code
        as a language, and linkage is neither located in nor edited, so a grammar
        gap there is out of its scope -- while a splice that broke real code
        lands outside linkage and is still returned.
        """
        if not self.root.has_error:
            return None
        node = _first_real_error(self.root)
        if node is None:  # every error was tolerated linkage
            return None
        at = node.start_byte
        window = self.data[max(0, at - _CONTEXT) : at + _CONTEXT]
        return Defect(
            offset=at,
            kind=node.type,
            missing=node.is_missing,
            context=window.decode("utf8", "replace"),
        )


def _root_of(node: Node) -> Node:
    """The program node a node belongs to -- how an edit finds its module.

    A node's ``id`` is the address of its C node and is distinct across trees,
    so the root's id names the module the node came from, and an edit built from
    it routes to that module and no other.
    """
    while node.parent is not None:
        node = node.parent
    return node


class Source:
    """The bundle as a language: every JavaScript module, spanned as one.

    Since 2.1.242 the app is dealt across well over a thousand modules; before it, one. This
    is that surface whichever it is -- ``find`` sweeps every module, ``count``
    sums across them, ``apply`` routes each edit to the module its node came
    from. A patch says ``source.find(name)`` and ``source.apply(edits)`` exactly
    as it did against a single module, because the one-module world *is* the
    many-module world with one module: nothing here is a case a patch has to
    know about.

    Immutable, like the modules it holds: ``apply`` returns a new `Source`
    sharing every module an edit did not touch and replacing only those it did,
    so the fixpoint runs each pass from the same pristine surface.
    """

    __slots__ = ("_entry", "_mods")

    def __init__(self, data: bytes, tree: Tree | None = None) -> None:
        # The single-module surface: a whole bundle handed over as one buffer
        # (a test, the pre-split monolith, a from-scratch reparse). It is the
        # many-module surface with one module, and every method below treats it
        # as exactly that.
        self._mods: tuple[_Module, ...] = (_Module(data, 0, tree),)
        self._entry = 0

    @classmethod
    def over(cls, modules: list[tuple[int, bytes]], entry_index: int) -> Source:
        """Span the given ``(blob index, bytes)`` modules, naming the entry.

        The entry module is where the manifest lives, so its position is kept; every module is one the container declared to
        be JavaScript (:meth:`patch_cc.bun.blob.Blob.js_modules`).
        """
        self = cls.__new__(cls)
        self._mods = tuple(_Module(data, index) for index, data in modules)
        self._entry = next(
            pos for pos, (index, _) in enumerate(modules) if index == entry_index
        )
        return self

    @property
    def data(self) -> bytes:
        """The entry module's bytes -- what carries the manifest.

        The manifest is appended to the entry module and read back from it
        (``read_manifest``/``is_patched``), a byte scan that never parses. For a
        pre-split build the entry *is* the whole app, so this is the whole
        bundle.
        """
        return self._mods[self._entry].data

    def contents(self) -> dict[int, bytes]:
        """Every module's current bytes, keyed by blob index -- for the write."""
        return {m.index: m.data for m in self._mods}

    # ------------------------------------------------------------ locating

    def find(self, name: bytes | str) -> Iterator[Node]:
        """Every node across every module that *is* this authored name.

        The name is the anchor -- a property name, a `case` label, an API
        string, a setting -- and the node it spells is what the edit is
        expressed against. Callers filter by ``node.type`` to say which *kind*
        of occurrence they meant: a `property_identifier` is a real property, an
        `identifier` a use of the same name.

        The name has to be the *whole* node, because bytes that merely occur
        inside a longer name are a different name and no later check sees the
        difference: `agentType` is not `subagentType`, and rewriting
        ``ya().spinnerTipsEnabledAt>0`` as a read of `spinnerTipsEnabled` gave
        ``!1>0`` -- a rewrite that counted, verified and parsed. `node.type` is
        no guard there either: the longer name is a `property_identifier` too.

        Prose that merely *contains* a name is :meth:`literals`' question.
        """
        needle = _bytes(name)
        for module in self._mods:
            yield from module.find(needle)

    def literals(self, text: bytes | str) -> Iterator[Node]:
        """Every string or template literal, across every module, carrying this text.

        The other half of :meth:`find`: a name is a whole node, but prose *has*
        no node of its own -- the product name inside a sentence, the opening
        words of a describe-string. Each literal is yielded once however often
        it says it.
        """
        needle = _bytes(text)
        for module in self._mods:
            yield from module.literals(needle)

    def module_index(self, node: Node) -> int | None:
        """The blob index of the module a node belongs to, or ``None``.

        Stable across the reparses `apply` does -- a module keeps its index
        through every edit -- so it is the key to ask "are these two nodes in the
        same module?" when the answer has to survive a step editing one of them
        (a program root's id does not: an edited module is a fresh parse). Only
        parsed modules are considered, and a node always comes from one.
        """
        target = _root_of(node).id
        for module in self._mods:
            if module.parsed and module.root.id == target:
                return module.index
        return None

    def find_local(self, anchor: Node, name: bytes | str) -> Iterator[Node]:
        """Every node that *is* this authored name, in ``anchor``'s module alone.

        A minified local is unique only within its module: the same two letters
        are a different variable in a thousand other chunks. So resolving one --
        a hoisted `agentType` constant read *through its value* to the declarator
        that binds it -- searches the module the read lives in, never the whole
        bundle, where :func:`only` would see a dozen unrelated declarations of
        the same spelling and rightly refuse to choose. Pre-split, the one module
        is the whole bundle, so this is exactly :meth:`find`; the two come apart
        only once the app is dealt across modules (2.1.242).
        """
        needle = _bytes(name)
        target = _root_of(anchor).id
        for module in self._mods:
            if module.parsed and module.root.id == target:
                yield from module.find(needle)
                return

    def count(self, name: bytes | str) -> int:
        """Raw occurrences of a literal across the whole bundle.

        Deliberately the *text* count, not a node count: a patch that reports
        `candidates` off its own matcher cannot distinguish "the shape moved"
        from "upstream retired the mechanism", and that distinction is the
        difference between a matcher to repair and a patch to delete. Summed
        over modules, so a marker a rewrite spliced into one module is found
        wherever it landed (`live-thinking` credits its core updates this way).
        """
        needle = _bytes(name)
        return sum(module.data.count(needle) for module in self._mods)

    # ------------------------------------------------------------- editing

    def apply(self, edits: Iterable[Edit]) -> Source:
        """Route each edit to its module, splice, and reparse -- or raise on rubble.

        A patch's batch may span modules (`custom-models` alone touches the enum,
        the validator, the resolvers, the redirect and the registry, in as many
        different chunks), and each edit knows its module through the node it was
        built from: the node's program root names the module, and every edit in
        that module is applied as one batch through :meth:`_Module.apply` -- the
        same disjointness check, high-offset-first ordering, incremental reparse
        and syntax gate a single module always had. One module or a thousand is
        the same code.
        """
        batch = list(edits)
        if not batch:
            return self
        roots = {m.root.id: pos for pos, m in enumerate(self._mods) if m.parsed}
        grouped: dict[int, list[Edit]] = {}
        for edit in batch:
            pos = roots.get(_root_of(edit.anchor).id)
            if pos is None:
                # The anchor belongs to no module of *this* Source: a node held
                # across the apply that already replaced its module, or from
                # another parse entirely. A patch builds its batch from the source
                # it is editing and commits it once, so this does not arise in a
                # run -- but routing a splice by a stale node is the one mistake
                # that would land it in the wrong module, so it is refused rather
                # than guessed.
                raise SyntaxGateError(
                    "an edit was built from a node that is not in this bundle"
                )
            grouped.setdefault(pos, []).append(edit)
        mods = list(self._mods)
        for pos, module_edits in grouped.items():
            mods[pos] = mods[pos].apply(module_edits)
        return self._with(mods)

    def append_manifest(self, text: bytes | str) -> Source:
        """Append the manifest comment to the entry module, through the gate.

        The same gated edit as any rewrite, so the bytes that get written are
        ones no splice -- the manifest's own included -- left unparseable, and
        the manifest travels with the entry module.
        """
        entry = self._mods[self._entry]
        return self.apply([Edit.at(len(entry.data), _bytes(text), within=entry.root)])

    def _with(self, mods: list[_Module]) -> Source:
        clone = Source.__new__(Source)
        clone._mods = tuple(mods)
        clone._entry = self._entry
        return clone

    # ---------------------------------------------------------------- gate

    def defect(self) -> Defect | None:
        """The first module whose *code* does not parse, or ``None`` if all do.

        The last guarantee in front of CONDUCT's first rule, and the only check
        that reads the bundle as a *language*. Counting rewrites cannot tell a
        splice that landed from one that landed a prop-name to the left, and the
        write verifier re-extracts what we wrote and compares it to what we meant
        to write -- so it agrees, byte for byte, with a bundle that cannot start.

        Every module is parsed here, which is what warms the whole surface for
        the patch run that follows: the trees this builds are the trees `find`
        reuses. Linkage errors are read past (:meth:`_Module.defect`).
        """
        for module in self._mods:
            found = module.defect()
            if found is not None:
                return found
        return None

    def fresh_defect(self) -> Defect | None:
        """Re-parse every edited module from scratch and return the first defect.

        The one place a parse from nothing runs, on the exact bytes about to be
        written and from a tree that shares nothing with the incremental one each
        edit was checked against. `_Module.apply` keeps that incremental tree in
        step and reparses at each batch -- fast, and correct on every build in
        the corpus -- but it is one lineage, trusted end to end; a parse from
        scratch is the only thing that would catch an incremental-reparse bug
        before it reaches the user's binary. Only the modules an edit touched are
        reparsed, so the cost is the edits' own, not the whole surface's.
        """
        for module in self._mods:
            if module.dirty:
                found = _Module(module.data, module.index).defect()
                if found is not None:
                    return found
        return None

    def verify(self) -> None:
        """Raise unless every module parses. Asked of the pristine source.

        The first gate rather than the last: :meth:`apply` holds every rewrite
        to introducing no defect, which leaves exactly one way a defective
        bundle could reach a write -- arriving that way. A build using syntax
        this grammar has never seen (beyond the module linkage read past on
        purpose) is not a patch to repair, so it is named as what it is, once,
        before the run it would otherwise indict.
        """
        found = self.defect()
        if found is not None:
            raise SyntaxGateError(
                "this build's bundle does not parse, so it cannot be patched "
                f"safely; the JavaScript grammar may be older than the build: "
                f"{found}"
            )


def _first_real_error(root: Node) -> Node | None:
    """The outermost error node that is not confined to module linkage.

    ``has_error`` is set on every ancestor of a defect, so this descends only
    through children that carry one -- reaching each error in tree *depth* steps
    rather than touching the millions of nodes a large module parses into. An
    error inside an import/export clause (`_LINKAGE`) is read past: the grammar's
    one gap is module aliasing, which is never code we edit.
    """
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            if not _in_linkage(node):
                return node
            continue
        stack.extend(
            child for child in node.children if child.has_error or child.is_missing
        )
    return None


def _in_linkage(node: Node) -> bool:
    """Is this error confined to an import/export statement's clause?"""
    current: Node | None = node
    while current is not None:
        if current.type in _LINKAGE:
            return True
        current = current.parent
    return False


# ----------------------------------------------------------------- walking


def text(node: Node | None) -> str:
    """A node's source, decoded. For comparing against names we wrote down.

    Takes "no node" so that reading a field its parent is *defined* by -- a
    call's callee, an `if`'s condition, a pair's value -- costs no narrowing
    branch for a case the grammar has already ruled out. It does not *answer*
    for one: an empty name compares equal to nothing, and splices into
    JavaScript that occasionally still parses (``renderer(C,{...})`` with the
    renderer missing is a valid comma expression), which is the one class of
    mistake CONDUCT's first rule cannot tolerate. So a climb that landed on
    nothing raises, and `Patch.run` reports that one patch broken.
    """
    spelled = None if node is None else node.text
    if spelled is None:
        raise ValueError("no node to read a name from")
    return spelled.decode("utf8", "replace")


def up(node: Node | None, *types: str) -> Node | None:
    """The nearest enclosing node of one of ``types`` (``node`` itself counts).

    How a patch gets from the name it anchored on to the node it means to edit:
    a `property_identifier` up to its `object`, a write up to the `if_statement`
    that is its arm, an anchor up to the `function_declaration` whose body is
    being replaced.
    """
    while node is not None and node.type not in types:
        node = node.parent
    return node


def climb(node: Node | None, want: Callable[[Node], bool]) -> Node | None:
    """The nearest enclosing node satisfying ``want`` -- ``up`` by predicate.

    For the ancestor that is identified by what it *contains* rather than by
    its type: the arm of a dispatch chain that also absorbs a message, the
    render call that also carries a witness prop.
    """
    while node is not None and not want(node):
        node = node.parent
    return node


#: What a block-scoped declaration (`let`/`const`/`using`) is confined to.
#: `for_statement` is here because a C-style loop's own binding
#: (`for(let i=0;;)` -- a real `lexical_declaration` in the head) is visible in
#: the head and body and nowhere else; `program` because the outermost region
#: has to be a node like any other rather than a case to notice.
#:
#: `for_in_statement` and `class_body` are deliberately absent, and structurally
#: rather than by measurement: a `for...of`/`for...in` binding is a bare
#: identifier the grammar hangs directly off the loop (never a declarator that
#: climbs to here), and no lexical declaration is ever a direct child of a class
#: body -- so both only ever named a region no binding could resolve to.
_REGIONS = (
    "statement_block",
    "switch_body",
    "for_statement",
    "program",
)


def _governs(declarator: Node) -> Node | None:
    """The region a declarator's names are visible in.

    Which region depends on how it was declared, and the grammar says: a `var`
    belongs to the function that holds it, a `let`, `const` or `using` to the
    block. The block-scoped spellings share one climb because they share one
    rule -- `using` is `let` with a disposal hook, not a third scope.
    """
    declaration = up(
        declarator, "lexical_declaration", "variable_declaration", "using_declaration"
    )
    if declaration is None:
        return None
    if declaration.type == "variable_declaration":
        return climb(declaration, _is_function_body)
    return climb(declaration.parent, of_type(*_REGIONS))


def _is_function_body(node: Node) -> bool:
    return node.type == "program" or (
        node.type == "statement_block"
        and node.parent is not None
        and node.parent.type in FUNCTIONS
    )


def visible(declarator: Node, site: Node) -> bool:
    """Is what this declarator binds in scope at ``site``?

    The question every spliced identifier owes an answer to, and the one kind
    of damage no gate here catches: an out-of-scope name parses, verifies, and
    throws when its line runs. Resolving scope from the site outwards is only
    half of lexical, and the half a subtree walk gets wrong: a declaration
    inside a nested function is invisible outside it, and so is one inside a
    plain block, which is the half stopping at function edges still gets wrong.
    A block-local ``useState`` in a component that declares no state of its own
    at the top level (the 2.1.232 conversation renders are two such components)
    would have an unscoped search report 2/2, splice a name from inside the
    block into a render outside it, and take the note that was the only signal
    with it.

    Position is asked for exactly one shape and no other: a *direct* read of a
    block-scoped binding, in the same execution scope, *before* the declaration
    runs. That is the temporal dead zone -- it parses, verifies, and throws at
    run time -- so it is the same class of damage as an out-of-scope name and
    the same one no other gate catches. Everything else keeps position out of
    it, because everything else is safe regardless of order: a closure written
    above a `let` reads it once invoked (bundle order is the bundler's, and
    selecting a site by "falls after the declaration" looks like a scope proof
    and is not one); a `var` is hoisted and has no dead zone; and a
    *scope query* -- "is this binding visible anywhere in this block?", the
    shape ``_options_bag`` and the reducer ask by passing the block itself as
    ``site`` -- is answered by containment, since every real read inside sits
    after the declaration whatever the block's own start byte is.
    """
    region = _governs(declarator)
    if region is None or not (
        region.start_byte <= site.start_byte and site.end_byte <= region.end_byte
    ):
        return False
    # A site that *encloses* the declaration is a scope query, not a use point:
    # its start byte is the block's `{`, before the declarator, so an order test
    # would reject the very binding it is asking about. Containment is the whole
    # answer there. Only a genuine point that is not a closure and not a `var`
    # owes the dead-zone check.
    encloses = (
        site.start_byte <= declarator.start_byte
        and declarator.end_byte <= site.end_byte
    )
    declaration = up(declarator, "lexical_declaration", "using_declaration")
    if encloses or declaration is None:
        return True
    site_fn = climb(site, of_type(*FUNCTIONS))
    decl_fn = climb(declarator, of_type(*FUNCTIONS))
    same_scope = (site_fn.start_byte if site_fn else -1) == (
        decl_fn.start_byte if decl_fn else -1
    )
    return declaration.end_byte <= site.start_byte if same_scope else True


def _walk(node: Node | None, scoped: bool) -> Iterator[Node]:
    """Every node at or under ``node``, outermost first.

    Iterative: a minified bundle nests deeply enough that a recursive walk is a
    recursion-limit bug waiting for the build that adds one more level.

    Nothing has nothing under it, so every search below inherits the same
    answer for a climb that came back empty -- which is what keeps "this shape
    is not on this build" flowing through the same code as the normal case
    instead of through a guard at each call site.

    ``scoped`` stops at the edge of a nested function, which is the difference
    between asking what a scope *does* and asking what it contains. `null-guard`
    asks the first: is this `if` the one that returns, or does it merely hold a
    callback that does? Reading a callback's ``return null`` as the arm's own
    deleted a conditional this patch has no business touching, and deleted it
    green. The reducer is the same question one size up -- three unrelated
    functions on 2.1.210 *contain* all three stream dispatches, and one of them
    performs them.

    Whether a *binding* reaches a site is not this question and cannot be
    answered by where the search started: see :func:`visible`.
    """
    stack = [node] if node is not None else []
    while stack:
        current = stack.pop()
        yield current
        stack.extend(
            child
            for child in reversed(current.children)
            if not (scoped and child.type in FUNCTIONS)
        )


def first(
    node: Node | None, want: Callable[[Node], bool], *, scoped: bool = False
) -> Node | None:
    """The first node at or under ``node`` satisfying ``want``."""
    return next((child for child in _walk(node, scoped) if want(child)), None)


def every(
    node: Node | None, want: Callable[[Node], bool], *, scoped: bool = False
) -> list[Node]:
    """Every node at or under ``node`` satisfying ``want``."""
    return [child for child in _walk(node, scoped) if want(child)]


def only(found: list[_T], what: str) -> _T | None:
    """The one thing of its kind, or nothing when this build carries none.

    For every site a patch *rewrites*, as opposed to every list it registers a
    name in: a rewrite has to know which node it means, and a bundle that
    suddenly offers two is not a tie to break. Absence stays the ordinary
    answer -- a shape this build lacks flows on through the same code -- but
    ambiguity raises, which `Patch.run` turns into one broken patch naming its
    own confusion.

    That is the promise the playbook makes ("fail closed and loudly when the
    invariants -- or their cardinalities -- change") held to at the one place it
    can be checked. Taking the first match instead is how a decoy wins: a
    prepended array carrying the four built-in model names would absorb the
    whole custom-model registration, real list untouched, every step green.
    """
    if len(found) > 1:
        raise ValueError(
            f"this build has {len(found)} {what}, and nothing here says which "
            "one is meant"
        )
    return found[0] if found else None


def children(node: Node | None) -> list[Node]:
    """A node's named children -- what it is *made of*, one level down.

    Unlike :func:`_walk`, this stops at the node's own parts, which is what a
    question about a list of siblings wants: the arms of a switch, the elements
    of an array, the properties of an object.
    """
    return list(node.named_children) if node is not None else []


def of_type(*types: str) -> Callable[[Node], bool]:
    """Predicate: the node is one of these kinds."""
    return lambda node: node.type in types


def returns(name: str) -> Callable[[Node], bool]:
    """Predicate: this statement is exactly ``return <name>``.

    "What it answers with is the value it was handed" is how several matchers
    here prove they found the right small function -- the effort whitelist, the
    picker's row assembler, the context-window resolver -- because a minified
    bundle offers no other way to tell one three-line function from another.
    A predicate rather than a phrase spelled per site, so the trailing ``;``
    a build may or may not emit is dealt with once.
    """
    wanted = f"return {name}"
    return lambda node: (
        node.type == "return_statement" and text(node).rstrip(";") == wanted
    )


# ------------------------------------------------------------ JS structures
#
# The handful of shapes every patch here actually edits. Each one dissolves a
# rule the playbook would otherwise have to state, because the grammar answers
# what a regex would have to assert.

#: Object literals and destructuring patterns. The same question -- "which
#: properties does this carry?" -- is asked of both, and upstream turns one into
#: the other freely (a props bag becomes a destructured parameter and back), so
#: they are one shape to :func:`props` and to :func:`owner`.
#:
#: They are two shapes to anything that *writes*: a literal passes a value and a
#: pattern binds a name, so a matcher looking for a prop to force says which of
#: the two it means -- ``of_type("object")`` -- rather than reaching for this.
_OBJECTS = ("object", "object_pattern")

#: Every way one property is spelled, and where each keeps its name. ``None``
#: is a property that *is* its own name.
#:
#: ``method_definition`` is not an exotic spelling to have remembered: a props
#: bag writes ``onChange:(v)=>{}`` or ``onChange(v){}`` interchangeably, and
#: which one ships is the minifier's choice rather than upstream's -- exactly
#: the half of a build that this module exists to make invisible. Leaving it out
#: made :func:`carries` answer "no" about a property plainly there: the 2.1.232
#: settings row carries `id`, `label`, `type`, `value` and `onChange`, and only
#: the four spelled with a colon were visible.
_KEYS = {
    "pair": "key",
    "pair_pattern": "key",
    "method_definition": "name",
    "shorthand_property_identifier": None,
    "shorthand_property_identifier_pattern": None,
}


def props(node: Node | None) -> dict[str, Node]:
    """An object literal or destructuring pattern as ``{name: value node}``.

    This is the primitive behind "a set is not a sequence". Property order,
    adjacency, and which properties sit *between* the ones a matcher cares
    about are all facts about a particular build; membership is the fact about
    the site. Two call sites listing the same properties in different orders
    would otherwise want a matcher each, and a single inserted property would
    kill both at once.

    A property with no ``value`` field -- shorthand (``{a}``), a method
    (``{a(){}}``) -- maps to itself, because it *is* what it names. So a caller
    asking for membership never has to know which spelling this build chose, one
    asking for the value gets the node that holds it either way, and
    :func:`entry` answers "where does this property start" for all three.
    Computed and spread properties have no authored name and are absent by
    construction.
    """
    found: dict[str, Node] = {}
    for child in children(node):
        if child.type not in _KEYS:
            continue
        field = _KEYS[child.type]
        key = child if field is None else child.child_by_field_name(field)
        if key is None or key.type == "computed_property_name":
            continue
        found[_name(key)] = child.child_by_field_name("value") or child
    return found


def _name(key: Node) -> str:
    """A property key as the name it spells, quotes removed."""
    spelled = text(key)
    if key.type == "string":
        return spelled[1:-1]
    return spelled


def owner(node: Node | None) -> Node | None:
    """The object literal or pattern a property name belongs to."""
    return up(node, *_OBJECTS)


def entry(node: Node) -> Node:
    """The whole entry -- ``name: value``, method, or shorthand -- a node is in.

    Where a new property is inserted: before a property *boundary*, so nothing
    computes where an object literal begins or ends. Every spelling ``_KEYS``
    knows is such a boundary -- a getter is a ``method_definition``, ``{a}`` is
    its own entry -- so this climbs all of them and falls back to the node only
    when it sits inside no property. Reaching just ``pair``/``pair_pattern`` read
    a getter-spelled ``/config`` row as no row at all, and the reader keyed on
    it (:func:`patch_cc.patches.chrome._shown_by_a_control`) then took that
    control for a gate and rewrote its own display (docs/PLAYBOOK.md).
    """
    return up(node, *_KEYS) or node


def key_of(node: Node) -> Node | None:
    """The name node of an entry, or ``None`` when it names nothing.

    Reads whichever field holds the key for this spelling -- a ``pair``'s
    ``key``, a ``method_definition``'s ``name`` -- while a shorthand *is* its
    own key. Anything that is not an entry (the fallback node :func:`entry`
    hands back for a value inside no property) has none. The mirror of the
    key-reading :func:`props` already does, so "which property is this" has one
    answer across the five spellings rather than a ``child_by_field_name("key")``
    that a getter silently answers ``None`` to.
    """
    if node.type not in _KEYS:
        return None
    field = _KEYS[node.type]
    return node if field is None else node.child_by_field_name(field)


def named(node: Node | None) -> Node | None:
    """The ``pair`` this node *names*, or ``None`` if it merely occurs in one.

    A property name is an anchor worth having only when it is the key: the same
    bytes appear constantly as a value, an argument, or inside a string, and a
    patch that took those for the property would edit whatever it landed in.

    Note two nodes are compared with ``==`` and never ``is``: every access
    builds a fresh wrapper around the same underlying node, so identity is
    always false and a check written with ``is`` silently rejects everything.
    """
    pair = up(node, "pair", "pair_pattern")
    if pair is None:
        return None
    return pair if pair.child_by_field_name("key") == node else None


def reads(node: Node | None, name: str) -> bool:
    """Is this node a member read of ``name``?

    The property is a field of the read, so this is one question with one
    answer -- where asking what the text *ends in* answers a different one,
    about the spelling of everything in front of it, and gets both wrong at
    once: ``x.subagentType`` ends in a name it is not, and ``x?.atis`` is the
    same read as ``x.atis`` through a different operator.
    """
    if node is None or node.type != "member_expression":
        return False
    property_name = node.child_by_field_name("property")
    return property_name is not None and text(property_name) == name


def written(node: Node | None) -> bool:
    """Is this read the *target* of an assignment rather than a value?

    A read is an expression and a value stands in for one; an assignment target
    is a *place*, so writing a value over one leaves ``!1=!1`` -- rubble the
    gate reports as that patch broken, taking every other rewrite in its batch
    with it. Two patches replace every read of a name they have an opinion about
    (`spinner-tips`, `thinking-summaries`), so the question has one home instead
    of being remembered at one of them and forgotten at the other.
    """
    parent = None if node is None else node.parent
    return (
        parent is not None
        and parent.type in ("assignment_expression", "augmented_assignment_expression")
        and parent.child_by_field_name("left") == node
    )


def is_true(node: Node | None) -> bool:
    """Is this node the boolean ``true``, however it is spelled?

    A minifier writes ``true`` as ``!0``, so a matcher that identifies a value by
    it (a bold badge's ``bold:!0``, an already-forced ``verbose:!0``) is reading
    the minifier's spelling -- the one half of a build this module exists to make
    invisible. Accepting ``true`` beside ``!0`` asks the value what it *is*,
    which survives a build that stops minifying that literal. Takes "no node" so
    a ``props(...).get(name)`` that came back empty is simply not true.
    """
    return node is not None and (
        node.type == "true" or (node.type == "unary_expression" and text(node) == "!0")
    )


def values(node: Node | None) -> Iterator[Node]:
    """The values this expression can stand for, by the grammar's own routing.

    A ternary answers with either branch, parentheses and a comma sequence with
    their last expression, an assignment with the value it assigns, and
    ``&&``/``||``/``??`` with either side; everything else -- a call, a read, a
    literal -- is a value of its own. Identity by membership among these is the
    rule the custom-model resolvers state for their rejection (2.1.234 wraps
    one in a dead recognizer, and the exact spelling reads a behaviourally
    identical build as the resolver being gone), asked here of any value a
    matcher has an opinion about: 2.1.246 mints ids for streamed blocks, so the
    element that was the block read becomes ``ce?{...S.contentBlock,id:V}:S.contentBlock``
    -- a value that can still *be* the block read, which an exact-node question
    reads as no block at all, with every anchor count standing.

    Kin to, and deliberately not one home with, ``patches.custom_models``'s null
    question: null routes *asymmetrically* through ``||``/``??`` -- a null on
    their left is exactly what both exist to pass over -- so what can arrive
    and what can arrive null are two tables, not one fact spelled twice.
    """
    if node is None:
        return
    if node.type == "ternary_expression":
        for field in ("consequence", "alternative"):
            yield from values(node.child_by_field_name(field))
        return
    if node.type in ("parenthesized_expression", "sequence_expression"):
        parts = [child for child in children(node) if child.type != "comment"]
        if parts:
            yield from values(parts[-1])
        return
    if node.type == "assignment_expression":
        yield from values(node.child_by_field_name("right"))
        return
    if node.type == "binary_expression":
        operator = node.child_by_field_name("operator")
        if operator is not None and text(operator) in ("&&", "||", "??"):
            for field in ("left", "right"):
                yield from values(node.child_by_field_name(field))
            return
    yield node


def receiver(node: Node | None) -> Node | None:
    """What a member read reads *from* -- ``a.b``'s ``a``.

    Takes "no node" for the reason :func:`text` does: a climb that landed on
    nothing has no fields either, and the caller that goes on to read a name
    off this is the one that raises.
    """
    return None if node is None else node.child_by_field_name("object")


def carries(node: Node | None, *names: str) -> bool:
    """Does this object carry every one of these properties?

    Identity by membership -- what a site *is*, rather than where it sits or
    what order this build happened to list it in.
    """
    present = props(node)
    return all(name in present for name in names)


def arguments(call: Node | None) -> list[Node]:
    """A call's arguments."""
    return children(None if call is None else call.child_by_field_name("arguments"))


def call_props(call: Node | None) -> dict[str, Node]:
    """The properties of the object a call is handed.

    Every render in this bundle is a component and a props bag, whichever
    runtime spelling the build uses -- ``createElement(C, props, ...)``,
    ``jsx(C, props)``, ``jsxs(C, props)``. What the bag *carries* is the site's
    identity; which function receives it is minifier noise.
    """
    for argument in arguments(call):
        if argument.type == "object":
            return props(argument)
    return {}


#: Every way upstream spells a function. A body replacement should not care
#: which, and the minifier converts between them.
FUNCTIONS = (
    "function_declaration",
    "function_expression",
    "arrow_function",
    "generator_function",
    "generator_function_declaration",
    "method_definition",
)


def conjuncts(node: Node | None) -> tuple[Node, Node] | None:
    """``(left, right)`` if this node is an ``&&``, else ``None``."""
    if node is None or node.type != "binary_expression":
        return None
    operator = node.child_by_field_name("operator")
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if operator is None or text(operator) != "&&" or left is None or right is None:
        return None
    return left, right


def binding(node: Node) -> Node:
    """The identifier a pattern binds, past any default value.

    ``{isTranscriptMode:i=!1}`` binds ``i``; the ``=!1`` is upstream's default
    and says nothing about the site.
    """
    left = node.child_by_field_name("left")
    return left if node.type == "assignment_pattern" and left is not None else node


def positional(node: Node | None) -> list[Node]:
    """A function's positional parameters, however it spells them.

    ``(a,b)=>`` and ``a=>`` are different fields in the grammar and the same
    thing to a caller; destructured bags are :func:`parameters`' question, not
    this one.
    """
    if node is None:
        return []
    params = node.child_by_field_name("parameters")
    if params is not None:
        return [
            child
            for child in children(params)
            if child.type in ("identifier", "assignment_pattern")
        ]
    single = node.child_by_field_name("parameter")
    return [single] if single is not None else []


def parameters(node: Node | None) -> dict[str, Node]:
    """What a function destructures from its parameters, by property name.

    An options bag is the same set-not-sequence question as any other object:
    upstream reorders it, adds to it, and moves properties in and out of it
    between builds, and none of that changes which parameter a name is bound
    to.
    """
    params = None if node is None else node.child_by_field_name("parameters")
    found: dict[str, Node] = {}
    for pattern in children(params):
        if pattern.type == "object_pattern":
            found.update({k: binding(v) for k, v in props(pattern).items()})
    return found


def body(node: Node | None) -> Node | None:
    """A function's body.

    Replacing the body node retires the whole class of break where upstream
    grows a statement inside it: 2.1.217 inserted a
    ``CLAUDE_CODE_DISABLE_EXPLORE_INHERIT_CAP`` escape hatch into the
    subagent-model bypass and silently cost every Explore override, because the
    matcher had spelled the body it expected. A body is a node; what upstream
    puts in it is upstream's.
    """
    return None if node is None else node.child_by_field_name("body")


def elements(node: Node | None) -> list[Node]:
    """An array literal's elements."""
    return [child for child in children(node) if child.type != "comment"]


def strings(node: Node | None) -> list[str]:
    """The string literals directly inside an array, as their values."""
    return [text(e)[1:-1] for e in elements(node) if e.type == "string"]


def dispatch(case: Node) -> int:
    """Where an update belongs at a ``switch`` arm's entry.

    Upstream's arm *bodies* are its busiest surface -- 2.1.226 threaded one flag
    through five arms of the stream reducer, turning ``stmt1,stmt2;`` into
    ``if(stmt1,stmt2,d)call;`` -- while the dispatch strings themselves
    (``"thinking_delta"``, ``"message_stop"``) are the API's vocabulary and do
    not churn. So an update valid at arm entry is spliced at the entry and says
    nothing about what the arm goes on to do.

    A label that carries no body of its own is one of a fall-through chain
    (``case"a":case"b":stmt``); the update belongs after the *last* label, so it
    runs for every label in the chain exactly as the body does, rather than for
    the first one only.
    """
    while True:
        label = case.child_by_field_name("value")
        statements = [child for child in case.named_children if child != label]
        if statements:
            return statements[0].start_byte
        following = case.next_named_sibling
        if following is None or following.type not in ("switch_case", "switch_default"):
            return case.end_byte
        case = following


def declared(node: Node | None) -> Node | None:
    """The statement a node is part of -- the unit an insertion sits before."""
    return up(
        node,
        "expression_statement",
        "lexical_declaration",
        "variable_declaration",
        "using_declaration",
        "return_statement",
        "if_statement",
    )
