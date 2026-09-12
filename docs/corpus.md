# The corpus

The measured claims in [PLAYBOOK.md](PLAYBOOK.md) — "every build in the corpus",
a counter that "moved 7 → 9", a matcher that "hit zero on all of them" — are
statements about a set of real Claude binaries. This file is that set, so a
claim is one command from re-verification instead of a number you have to trust.

## What it is

Before the first patch of a version, patch-cc copies the pristine binary to
`~/.local/share/patch-cc/backups/<version>.orig` ([INTERNALS.md](INTERNALS.md#safety)).
The corpus is exactly those copies: not a fixture checked into the repo (each is
a few hundred MB), but a directory this machine fills. Left alone it fills
**unevenly** — a build patched here leaves its `.orig`, a build that shipped while
this machine sat idle leaves nothing — so the set samples an update cadence rather
than a release history, and its holes are invisible from inside it.
`scripts/corpus.py` closes that: Anthropic still serves every build it published,
so the set is completed off the artifact, like everything else here.

The binaries are the artifact, not this file — `doctor` recomputes every count
from them, so nothing here can drift from what a matcher actually does. Nor is
this file where "pristine" is defined: every release manifest carries a
per-platform sha256 and each `.orig` is checked against it. The table below is
where that check is written down once per build, so its hashes are the offline
record for builds Anthropic may one day stop serving — never a second authority
to keep in step.

## On disk now

One row per pristine binary held, appended as builds ship and never restated — so
a hash that stops matching means the file changed under you, not that a number
moved. A gap in the numbering is upstream's rather than a miss: not every version
reached the release channel, and `corpus.py status` names which, so no list of
them is kept here to go stale. The set straddles the **2.1.242 code split**
(INTERNALS.md): `2.1.242`/`2.1.243` are the first many-module builds, and they
jump ~35 MB over `2.1.241` for it. Sizes repeat across builds, which is why the
identity column is the hash (of the whole file, `sha256sum <version>.orig`) and
never the size:

| version | size | sha256 |
|---|---|---|
| `2.1.210` | 261 MB | `e7d2ceb53ed4c2ced1fe7fc1c6331c98dc5f7b4c9b2722d9c5fa3dd5dff6f719` |
| `2.1.211` | 262 MB | `8272c8a474ac9ea1bc35f19b9f7c7e7dc4dc4eb6d5ad3e484b19335ac72446b2` |
| `2.1.212` | 264 MB | `044a88cf3a5180776617fd3da1238dcbf9141ddec449a39cf7d2af1ac78e684e` |
| `2.1.213` | 265 MB | `7999631426e1b6e4444e4ecf9cd8a63a05a0411ccfe503927d4c9d57bc41bc64` |
| `2.1.214` | 265 MB | `3c029136f7c81f54ed4a38e9d52e655aad536433dbbde50519c8c31bb646ad14` |
| `2.1.215` | 265 MB | `c1efffaaf370aa187cb6a09dd93d4e511c646899b0078476f83791b664bde7fe` |
| `2.1.216` | 267 MB | `74deca45220b8080ec75ab099bd5a5980e41a2b5879846a008fb115d436de085` |
| `2.1.217` | 269 MB | `2630fc5dc6db61bc03f86b95daf47766e5ed5b61873f7bb7cfea764c5ac5a9ba` |
| `2.1.218` | 273 MB | `e12071751a9336b8af1012c103358ff04ac18f9aaff4a738cff7ba5cdfaf63f2` |
| `2.1.219` | 275 MB | `22cfd6f5b3061c0391ba84e9cf8c9deaa37783aac18b004d42ec061e98f00691` |
| `2.1.220` | 275 MB | `674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863` |
| `2.1.221` | 289 MB | `60db8e88d42c24b5199c92cfd56ec88370c510c3789c6f364af748354f087ada` |
| `2.1.222` | 289 MB | `10caae8f22b915c26bfff0e013a4d45608c4f1ae287583626569156f447730e5` |
| `2.1.223` | 291 MB | `98226474f802e3094d6a86c5ade8883c16206d0fcb5c400b7401c800063e99d7` |
| `2.1.224` | 296 MB | `a2b5add7dc4bcd8eaa029f4e8bdac4df7769b4073698db7989d206baf9419c2d` |
| `2.1.225` | 298 MB | `0a3be8d18cb0f5357d38ce2d588601753a60b44cc9c622579ed8b8405dee231e` |
| `2.1.226` | 298 MB | `4e9bec1177ce9690e8bd988b710ac24105e70da428dd094c5adcbbe786a55555` |
| `2.1.227` | 304 MB | `6832dc3f1797b890b71116e5f2dbbf9a83fd3d0498c235b4b0f9cd0e6e499ad6` |
| `2.1.228` | 309 MB | `d535985e6941a3eb00179ccd7f52ceb0c6623a0305a518ebc4e6514f84a94c99` |
| `2.1.229` | 311 MB | `200338139a3df04a9ad22233837d1fb53fb6dffa21cd82e47559bfaa115acc1b` |
| `2.1.231` | 311 MB | `47a01daebf794f6c86c13d1875ad6e5be0627029ad8600731161f24018ecde5b` |
| `2.1.232` | 323 MB | `61d23f8749136907d586d5b11831ea8a5234d4c1dea40a5e55c33b52e204c6d1` |
| `2.1.233` | 325 MB | `55d281096f57d411ebbdd94dbf5e9ff3accb7c05713e37348c2c11d4b83bf9d9` |
| `2.1.234` | 328 MB | `3473601ea695d5bf769c5b202844d4cb4fbf723ae995450fcb6973204775c84a` |
| `2.1.235` | 331 MB | `bfcf0ae2dbf94b2b6a106074aabf3938b9a10889c3b678e4cb5a00c03274d5d5` |
| `2.1.236` | 335 MB | `6c8818fa22187aa555c242be4abbacc44d6b71a32ac9631ee7b2b5d12f51f752` |
| `2.1.237` | 335 MB | `73975167f0108693cf6fd6614994781657ebb8456ebef5d247458734abfb3916` |
| `2.1.238` | 339 MB | `0933b286cf94e1b2504b35ac165ab76b8f822735d53371c56393988c23040d58` |
| `2.1.239` | 343 MB | `7de1b1576e2e0be73ce91c2b4dedf16a41058ea633b957a36fdc6044ddfc0f3c` |
| `2.1.240` | 343 MB | `1386169da77de19a655f07a86ab80f5775983a50eb0c9c27a7daf16e7320322d` |
| `2.1.241` | 343 MB | `0771bd866cff82b76581fc0499f6529e1a36845078f144f8c81dccb3bc7037b8` |
| `2.1.242` | 378 MB | `528ef039aa7d64d7b3fbc06925132755a516b4dcaad784cf0b51fe03167360d4` |
| `2.1.243` | 378 MB | `4b0dafeedd0b469c41988e200036fd773e7553ba960349c9f02a82c6d1f2ba27` |
| `2.1.245` | 392 MB | `16ad2b94deaf7b29abed966d981c9991a47af0420f5be8ed4a3f83bea9f678bc` |
| `2.1.246` | 248 MB | `1a0a662dc1bb938eaec38545abce9a4a69113d7d7f7c5e1a553ea276617b906a` |
| `2.1.247` | 250 MB | `5fb321bf417ffc5cd4e3f36e7c9c7e029bf47aaa36d5621db979fcc5e6eabe15` |
| `2.1.248` | 224 MB | `3edee3cb054bd6823674fd60d5c0e442825b28ee8fbf815af2d16bf0de072e16` |
| `2.1.250` | 224 MB | `2be252a00ac56e704d7fbf7e5e9ef1243584093334a861945238a0c27e84bdac` |
| `2.1.251` | 214 MB | `fd5f10ff0eb58daec04900466b143ea98aab50abf208a422bc008eaec13f61f7` |
| `2.1.252` | 214 MB | `a715a45105e593fc9808d035d77781f88480b9897975a9df41837f0c591bd4b3` |
| `2.1.257` | 215 MB | `9a64bda9d8722a1fa05bef9a5961d07e0331b99597eda9e2f6a732f3a0ff7f05` |
| `2.1.258` | 215 MB | `704f1334ac65d3e89e1c6c1d7663293ad786a6166afdb71b5075337df630f976` |
| `2.1.259` | 217 MB | `f7dd62ae415378018cd21dd950eb3bac174ab085830304d3b8b098146bfd47b6` |
| `2.1.260` | 215 MB | `7a2fdc74b6836ea3d183f665b869f0ee3baebc9713cbebffe5838da4ea7bd82e` |
| `2.1.261` | 216 MB | `4ae40dd1784e85753e742e09f267d29ecbb82890361ad3817d27560866d364a6` |
| `2.1.263` | 216 MB | `26d020351e8112f4006790f3cfce43b4c9df0c1bb1d0e542364d64151b81d5ba` |
| `2.1.265` | 216 MB | `e14738e3a58d1fc6ccc23b9c919451b4846bc27074a3fb48db976a7d595bdeeb` |
| `2.1.266` | 216 MB | `19842705e989393fce936804df6d2ab034860e24b8f8880357981d87ffd83fac` |
| `2.1.267` | 217 MB | `0399c793ff571d5946ef923d80b4f330d05ac4b6842a6b0775468f5d389403c0` |
| `2.1.268` | 219 MB | `9691a2b7bd796712ca8cffb8e32e54ff7fc45b662540233171a16a94a0425653` |
| `2.1.269` | 220 MB | `25e44883f54419569a3d739f38cbbdaebe83b09895da0f343e1b003710a4775b` |

This set covers the span the playbook's tree-move measurements were taken over
(`2.1.210` → `2.1.233`), the 2.1.242 split, the 2.1.246 stream-store
migration, the 2.1.247 selector reads it was read back through, the
2.1.257 compiled transcript renderer that retired live thinking's render half,
and the 2.1.269 Bun 1.4.3 records that retired the container's record walker, so
each is re-checkable here rather than historical.

## Keep it complete

```bash
uv run scripts/corpus.py            # what upstream serves, against what is held
uv run scripts/corpus.py sync       # fetch every published build the set lacks
```

`status` is read-only and exits non-zero when the set is short or damaged. It
keeps five answers apart rather than counting them together ([CONDUCT.md](CONDUCT.md)):
`ok`, `missing` (served, not held), `unpublished` (never served — a gap, not a
miss), `unverifiable` (held, no longer served, so nothing left to check it
against) and `corrupt` (held, and not what upstream shipped). `sync` acts on
`missing` alone: a `corrupt` entry is evidence that something here damaged a
binary, and replacing it silently would destroy the only sign of that — delete it
and re-run to take a fresh copy.

The span walked is the oldest build held → the channel's head, so no version
number is written down anywhere; `--since` moves the floor, and builds already
held are read, tabled and swept whatever the span says. A machine holding
nothing has made no statement about how far back it cares, so the walk starts at
the head — `--since` is how a fresh checkout asks for history. A build this machine
never patched can still join the old way — save its pristine native binary as
`~/.local/share/patch-cc/backups/<version>.orig` — and the next run verifies it
like any other. To read the JS a given binary carries without patching anything:

```bash
patch-cc extract ~/.local/share/patch-cc/backups/2.1.233.orig > 2.1.233.js
```

## The sweep

The whole point of the set — every patch against every build, the exit codes and
the `diff` between two revisions ([PLAYBOOK.md](PLAYBOOK.md#repairing-a-broken-patch)):

```bash
sweep() { for b in ~/.local/share/patch-cc/backups/*.orig; do
  echo "== $(basename "$b")"; patch-cc doctor "$b" || true
done; }
sweep
```

`doctor` never writes to the backup it reads — it runs the matchers, parses
their output, then bakes the composed result into a *temp* binary and executes
`<binary> --version` (the smoke run, [PLAYBOOK.md](PLAYBOOK.md#how-resilience-is-detected)),
removing the temp afterwards — so the sweep is safe to run against every backup
at any time, and proves each build bakes and boots, not just that it matches.
Each bake transiently writes a binary-sized temp file (a few hundred MB) to the
system temp dir — RAM, where that is tmpfs — one at a time; `TMPDIR` steers it
elsewhere.
