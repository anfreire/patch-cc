#!/usr/bin/env python3
"""Fetch the corpus: make the set of pristine builds the published set.

The measured claims in [PLAYBOOK.md](../docs/PLAYBOOK.md) are statements about a
set of real Claude binaries ([corpus.md](../docs/corpus.md)). That set accretes
on its own -- every first patch of a version leaves a ``<version>.orig`` -- which
means it is sampled by how often this machine happened to update, never by what
Anthropic actually published. Five builds -- 2.1.258-2.1.261 and 2.1.265 -- were
missing here for exactly that reason, and they span the window the last two
live-thinking repairs were made blind to.

So the corpus is completed the way everything else here is decided: off the
artifact. Anthropic still serves every build it published -- 2.1.210 included --
and each release manifest carries a per-platform sha256, which is what "pristine"
means: upstream's to state, never ours to keep a second copy of.

One walk is the whole tool:

    for each version the corpus holds, and each one between its floor and the
    release channel's head, what does upstream serve and what is on disk?

``status`` reports that walk and ``sync`` acts on it. That is all it does. What
to do *with* a complete corpus -- the sweep, the diff between two revisions -- is
[PLAYBOOK.md](../docs/PLAYBOOK.md)'s, and is unchanged by this existing; this
just makes sure the set that workflow runs over is the whole one. Nothing here
knows a version number either: the span is read from the corpus and the channel,
so there is nothing to update when Claude ships.

    uv run scripts/corpus.py [status|sync]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from patch_cc.patcher import backup_dir

#: Where releases live. The installer's own base URL (``claude.ai/install.sh``),
#: which is the contract Anthropic publishes for anyone downloading a build. The
#: GCS bucket behind it is an implementation detail that
#: ``.github/workflows/release-watch.yml`` reaches for directly, and deliberately
#: keeps its own spelling of: that workflow never checks the repo out -- a tick
#: there is one curl, every ten minutes -- so sharing this string would cost it a
#: clone to save a line.
CHANNEL = "https://downloads.claude.ai/claude-code-releases"

#: Transports, most compressed first. Purely a bandwidth choice -- 76 MB against
#: 216 MB on 2.1.266 -- and never a second way to be right: whichever arrives,
#: what gets hashed is the finished binary against ``manifest.json``. A build that
#: fails that check never becomes a corpus member, so the fast path cannot land
#: bytes the slow one would have refused.
TRANSPORTS = (("claude.zst", "zstd"), ("claude", ""))

#: What a version can be. Five words rather than one count, because they fail
#: differently and a report that collapses them has thrown away the distinction
#: it exists to draw ([CONDUCT.md](../docs/CONDUCT.md)). What each one *means* is
#: [corpus.md](../docs/corpus.md)'s to say -- that is where a reader meets these
#: words, printed by ``status`` -- so it is defined there and not restated here.
OK = "ok"
MISSING = "missing"
UNPUBLISHED = "unpublished"
UNVERIFIABLE = "unverifiable"
CORRUPT = "corrupt"

Version = tuple[int, int, int]


def parse(text: str) -> Version | None:
    """A dotted triple, or ``None`` for anything that is not one.

    The channel is a text file and a backups directory is a directory: either can
    hold something that is not a version (an HTML error page, a hand-named
    ``.orig``), and neither is worth raising over. Declining to parse is how such
    a thing stays out of the walk.
    """
    parts = text.strip().split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    major, minor, patch = parts
    return int(major), int(minor), int(patch)


def show(version: Version) -> str:
    return ".".join(str(part) for part in version)


def target() -> str:
    """This machine's platform key, spelled the way the installer spells it.

    Mirrors ``claude.ai/install.sh``: the manifest is keyed by these names, so
    they are upstream's vocabulary and not ours to improve. Getting it right is
    what makes verification mean anything on a Mac -- a corpus checked against
    ``linux-x64`` there would read every build as corrupt.
    """
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"
    if platform.system() == "Darwin":
        return f"darwin-{arch}"
    musl = "musl" in (platform.libc_ver()[0] or "") or any(
        Path(f"/lib/libc.musl-{name}.so.1").exists() for name in ("x86_64", "aarch64")
    )
    return f"linux-{arch}-musl" if musl else f"linux-{arch}"


def get(url: str, into: Path | None = None) -> bytes | None:
    """Fetch a URL. ``None`` means upstream does not have it, and nothing else.

    Only a 404 becomes ``None``; every other failure is raised. The walk reads
    absence as "never published" and writes that into the corpus document, so a
    flaky 500 quietly read as a gap would retire a build from the set that
    Anthropic is still serving.

    With ``into`` the body is streamed to that path and an empty one is returned:
    a release is a few hundred megabytes, and the only caller that wants it in
    memory is the one asking for a manifest.
    """
    try:
        with urllib.request.urlopen(url, timeout=120) as response:
            if into is None:
                return bytes(response.read())
            with into.open("wb") as handle:
                shutil.copyfileobj(response, handle)
            return b""
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def checksum_of(version: Version, plat: str) -> str:
    """What upstream says this build's bytes are, or ``""`` if it serves none."""
    raw = get(f"{CHANNEL}/{show(version)}/manifest.json")
    served = {} if raw is None else json.loads(raw)
    return served.get("platforms", {}).get(plat, {}).get("checksum", "")


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def held() -> dict[Version, Path]:
    """Every ``<version>.orig`` in the backups directory, by version.

    The directory is patch-cc's own (:func:`patch_cc.patcher.backup_dir`), so the
    corpus is read from the one place that defines where it lives, and a backup
    under a name that is not a version simply is not corpus.
    """
    root = backup_dir()
    found = {}
    for path in sorted(root.glob("*.orig")) if root.is_dir() else ():
        version = parse(path.name.removesuffix(".orig"))
        if version is not None:
            found[version] = path
    return found


def span(floor: Version, head: Version) -> Iterator[Version]:
    """Every candidate version from the corpus floor to the channel head.

    The bucket refuses anonymous listing, so there is no set to read and the
    candidates have to be walked. Two bounds make that a statement rather than a
    guess: the floor is the oldest build the corpus already keeps -- the
    maintainer's own answer to how far back matters -- and the head is what the
    channel reports today. Between them a release series is its patch numbers,
    and a 404 among them is upstream's gap, not our miss.

    One invariant is declared rather than guessed at: **the floor and the head
    are in the same release series**. Where a series ended is not something the
    channel will tell you and not something a run of 404s can prove -- the corpus
    already holds a four-build gap (2.1.253-2.1.256) -- so once 2.2 ships this
    refuses instead of inventing a boundary. ``--since`` is the answer, and by
    then the older series is complete on disk, where :func:`held` keeps it in
    every reading regardless of the span.
    """
    if floor[:2] != head[:2]:
        raise SystemExit(
            f"the corpus starts at {show(floor)} and the channel is at {show(head)}: "
            f"where {floor[0]}.{floor[1]} ended is not something this can know.\n"
            f"Walk the current series with `--since {head[0]}.{head[1]}.0`; the "
            "builds already held are read and swept either way."
        )
    return (floor[:2] + (patch,) for patch in range(floor[2], head[2] + 1))


@dataclass(slots=True)
class Entry:
    """One version's answer to the walk: what upstream serves, what is on disk."""

    version: Version
    state: str
    #: The bytes upstream says it serves, empty when it serves none -- and what
    #: :func:`fetch` checks a download against, so the walk that found a build
    #: missing is also what proves the copy that replaces it is upstream's.
    upstream: str = ""

    @property
    def name(self) -> str:
        return show(self.version)


def classify(version: Version, path: Path | None, plat: str) -> Entry:
    """One version, against upstream. Five outcomes, none folded into another."""
    want = checksum_of(version, plat)
    if path is None:
        return Entry(version, MISSING if want else UNPUBLISHED, want)
    state = UNVERIFIABLE if not want else OK if digest(path) == want else CORRUPT
    return Entry(version, state, want)


def survey(floor: Version | None = None) -> list[Entry]:
    """The walk itself: every candidate, classified, in version order.

    The candidates are the span *and* everything the corpus already holds. The
    union is what keeps the two readings honest at once: the span decides whether
    the corpus is complete, while a build on disk is never dropped from a sweep or
    a table for sitting outside it -- which is precisely what ``--since`` does to
    an older series.

    Probes run concurrently because they are independent and there are dozens; the
    result is re-sorted, so what comes out is ordered by version and not by which
    request answered first. A sweep's diff depends on that.
    """
    plat = target()
    have = held()
    head = parse((get(f"{CHANNEL}/latest") or b"").decode())
    if head is None:
        raise SystemExit("the release channel did not answer with a version")
    floor = floor or (min(have) if have else head)
    versions = sorted(set(span(floor, head)) | set(have))
    with ThreadPoolExecutor(max_workers=16) as pool:
        entries = pool.map(lambda v: classify(v, have.get(v), plat), versions)
    return sorted(entries, key=lambda entry: entry.version)


def megabytes(size: int) -> str:
    """Decimal MB, the unit the corpus document's size column is written in."""
    return f"{round(size / 1_000_000)} MB"


def fetch(entry: Entry, plat: str) -> None:
    """Download one build and land it only once it is upstream's bytes.

    Staged under a partial name and renamed only after the hash matches, for the
    same reason patch-cc never writes a binary it has not re-read
    ([CONDUCT.md](../docs/CONDUCT.md)): an interrupted download left as
    ``<version>.orig`` would be a corpus member that quietly is not pristine, and
    every count taken against it afterwards would be wrong in a way no sweep can
    report.

    Landed executable, because a corpus member is not a blob: ``doctor`` bakes it
    into a temp binary and *runs* it, and the bake inherits the mode it read. A
    download arrives ``0644`` where patch-cc's own backup is a ``copy2`` of an
    installed ``0755`` binary, so the first sweep over five freshly fetched builds
    had every matcher green and every smoke run dead on ``Permission denied`` --
    the 2.1.246 shape exactly, correct bytes that will not execute. The bit is set
    on the staged file, so no member ever wears the wrong mode even briefly.
    """
    root = backup_dir()
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{entry.name}.orig"
    staged = dest.with_suffix(".part")
    print(f"  {entry.name:9s}", end="", flush=True)

    for name, tool in TRANSPORTS:
        if tool and not shutil.which(tool):
            continue
        if get(f"{CHANNEL}/{entry.name}/{plat}/{name}", into=staged) is None:
            continue
        if tool:
            subprocess.run(
                [tool, "-q", "-d", "-f", str(staged), "-o", f"{staged}.out"],
                check=True,
            )
            Path(f"{staged}.out").replace(staged)
        break
    else:
        raise SystemExit(f"no transport served {entry.name} for {plat}")

    got = digest(staged)
    if got != entry.upstream:
        staged.unlink()
        raise SystemExit(
            f"{entry.name}: checksum mismatch\n"
            f"  upstream {entry.upstream}\n  received {got}"
        )
    staged.chmod(0o755)
    staged.replace(dest)
    print(f"  {megabytes(dest.stat().st_size):>7s}  ok")


def cmd_status(entries: list[Entry]) -> int:
    """Render the walk, and answer with whether the corpus is whole."""
    order = (CORRUPT, MISSING, UNVERIFIABLE, UNPUBLISHED, OK)
    grouped = {state: [e.name for e in entries if e.state == state] for state in order}
    print(f"corpus    {backup_dir()}")
    print(f"platform  {target()}")
    print(
        f"span      {entries[0].name} -> {entries[-1].name}, {len(entries)} considered"
    )
    print()
    for state in order:
        names = grouped[state]
        listed = f"  {' '.join(names)}" if names else ""
        print(f"  {state:14s}{len(names):4d}{listed}")
    print()
    if grouped[CORRUPT]:
        print("corrupt entries are not what upstream shipped; delete them and re-sync.")
    if grouped[MISSING]:
        print(f"`uv run scripts/corpus.py sync` fetches {len(grouped[MISSING])}.")
    return 1 if grouped[CORRUPT] or grouped[MISSING] else 0


def cmd_sync(entries: list[Entry]) -> int:
    """Fetch what upstream serves and the corpus lacks.

    Only :data:`MISSING` is acted on. A :data:`CORRUPT` entry is reported and left
    alone: it is evidence -- disk rot, an interrupted copy, the wrong file saved
    under a version's name -- and replacing it silently would destroy the only
    sign that something on this machine damaged a binary.
    """
    plat = target()
    wanted = [entry for entry in entries if entry.state == MISSING]
    damaged = [entry.name for entry in entries if entry.state == CORRUPT]
    print(f"fetching {len(wanted)} for {plat}" if wanted else "corpus is complete.")
    for entry in wanted:
        fetch(entry, plat)
    if damaged:
        print(f"\ncorrupt, left untouched: {' '.join(damaged)}")
        print("delete them and re-run to replace them from upstream.")
    return 1 if damaged else 0


VERBS = {
    "status": cmd_status,
    "sync": cmd_sync,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="corpus.py",
        description=(__doc__ or "").split("\n\n")[0],
    )
    parser.add_argument("verb", nargs="?", default="status", choices=sorted(VERBS))
    parser.add_argument(
        "--since",
        metavar="VERSION",
        help="walk from this version instead of the oldest build already held",
    )
    args = parser.parse_args(argv)
    floor = parse(args.since) if args.since else None
    if args.since and floor is None:
        parser.error(f"--since wants a dotted version, not {args.since!r}")
    return VERBS[args.verb](survey(floor))


if __name__ == "__main__":
    sys.exit(main())
