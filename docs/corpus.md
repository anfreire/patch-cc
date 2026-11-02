# The corpus

The measured claims in [PLAYBOOK.md](PLAYBOOK.md) — "every build in the corpus",
"the same single site on every build", "thirty-four builds carry the surface" —
are statements about a set of real Claude binaries. This file is that set, so a
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

## The builds

One row per pristine binary, appended by the [release watch](../.github/workflows/release-watch.yml)
once `doctor` passes on it, and never restated — so a hash that stops matching
means the file changed under you, not that a number moved. A gap in the
numbering is upstream's rather than a miss: not every version reached the
release channel, and `corpus.py status` names which, so no list of
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
| `2.1.270` | 224 MB | `3a624a5a7cd79bbad4d32bd7db36f1197ecf458bc5bf1e2aed81834a01ad3ef0` |
| `2.1.271` | 227 MB | `5e7b6fc24d0e124f68e99a0641c47b662945c6d197d768d7ad7a72cbf1897f58` |
| `2.1.272` | 227 MB | `d81396a668eb76fbddb49a2a5841f1b5d7af96b4c1f6500ced92f2c988f5bcd4` |
| `2.1.273` | 229 MB | `6c752e2cc7c110c9df15f26d8d134d438c5ae95dbd610efc1a308bf7f9c5f6c1` |
| `2.1.274` | 231 MB | `15e2d05148f801b5774032faad87e624ecd172e9903288bda448b892eb58fa07` |
| `2.1.275` | 232 MB | `13586f3150a7ca1655f36e1dba759fb404e0f7cf7021d4a3dcd5e6f604e56156` |
| `2.1.276` | 232 MB | `8a56c8a14bd3cb246e2bdb7e60aefe0f609bff78c8bbcc5ea6b1817c111c6145` |
| `2.1.277` | 234 MB | `722210f05ba494d8f6df69423c4d4f2960900f7a007d0532851c7a36e375cab7` |
| `2.1.278` | 234 MB | `5c4735937844e84f8a93306e841a5b0e12252909b07870f789b190468da147ab` |
| `2.1.280` | 234 MB | `1e08503dbdf3c2cb0d706d32f3408277388d1c76ef108673e8fe42c1b322925b` |
| `2.1.281` | 237 MB | `56fe3da88458465fb27d7e9299dddb3fead55750fb9c2de795f233b5eea6dce1` |
| `2.1.282` | 239 MB | `3afe8535c0cc33f0e24f7b25dab7a1727b8b592196f8496a8bc302ba2161eed3` |
| `2.1.283` | 242 MB | `1859583ce32920595c61ef868bee52e1b1594f7486db209935e01f1e5e804ae2` |
| `2.1.284` | 243 MB | `5cd90aabd83f8a15136c35aa37bb1d92b348993573316643dc3fe4e04afbf88f` |
| `2.1.285` | 240 MB | `33dad1ec615a2e08cc78b494f05c110e49916de2c79d78ec8799ebf46b233d29` |
| `2.1.286` | 242 MB | `fe503f65c6289d59c23e5b21ae44f03583f997dd33a2cbfc75ab4f96fb8fc73f` |
| `2.1.287` | 244 MB | `3920489a5109cff5786a1a392c25277408ff22bc796d5edb9c16a60e5a1718f0` |
| `2.1.288` | 246 MB | `0298068b686e7fdbaf9402a7a587bb7f49c0b0e084de09f69145a0719207640c` |
| `2.1.289` | 246 MB | `a186b99e4a9c88366cd49df2f7dad56c61fc306ef0140b19ee64b7c42a8d1348` |
| `2.1.290` | 250 MB | `ea38ee1a1f946eea9bc6e97fb912dbe71fc379b25cc605d91b308afa1ca08be7` |
| `2.1.291` | 250 MB | `078fad28d0297c9a25d306b635b2d8816c6839347520f29eb54ffea5d56142fb` |
| `2.1.292` | 251 MB | `a967e7b1d8b4e47ee421d5433027880347952b0c0857abf880e2c942a4ec93b3` |
| `2.1.293` | 253 MB | `8968405e26db478af44eabc4635ab5ca557057b702a54460a59c13e1b253e978` |
| `2.1.294` | 253 MB | `27122ca7b624f537546fbef35b80c66370d974ff258f3d9b10ac50bb8771f262` |
| `2.1.295` | 256 MB | `4503bfe11a6c7fcc1e0b39b5e0d347c04248f750b03b0977b3ad6b531fe6f358` |
| `2.1.296` | 257 MB | `24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de` |
| `2.1.299` | 265 MB | `2d894a4b1bfaaa9aab72aeff7803e3c73bc2510f279a171f6b8f59f72d17d077` |
| `2.1.300` | 267 MB | `674deba0bba9aee479f4503c438b49303b6fdd304b9a471b14977aaec5215453` |
| `2.1.301` | 268 MB | `bc1aad9982c7b59c7d889ff92595d61cf46e0a6eae074cb551d932a989bcd222` |
| `2.1.302` | 286 MB | `60d7eb7f12fbb28a52533fe8be5884097403562aa02dc73991a021c7067e29a5` |
| `2.1.303` | 286 MB | `375988f457756524234695cf2d8f64ace99c5f68682e47ea24862a843c191450` |
| `2.1.304` | 289 MB | `ea0f135784631619a7a409d07322ecec964f9cc4fd377508ed1ba3f141f27912` |
| `2.1.305` | 291 MB | `86cb722ab68348877dba7971e4d0c659919e05e6228c05e006e83da320e656ef` |
| `2.1.306` | 291 MB | `520bc6c1f40af466bd03a278b5ae1af1a2938bc7de5b9e5cb15526da20d71cfe` |
| `2.1.307` | 292 MB | `6021eb72d20d0682b720bdc63acd2a5cd2270895d1f10d9ca2a88cbfa7b9dd0e` |
| `2.1.308` | 292 MB | `356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58` |
| `2.1.309` | 293 MB | `4d704a8aecbe038affa3000f498ae17c1b4f753add8facb8764120affc99b658` |
| `2.1.310` | 292 MB | `b73676430af12f97316a798b8371f81d640f90a58bdcb8f3ce56a835bb47df56` |
| `2.1.311` | 294 MB | `86dc1fcc69498159eaf15f446613e847010f9036bfbbdffd4c49350be8489ada` |
| `2.1.312` | 294 MB | `4f4178ce41d83e23341ebfbaecdbcf8ea6bf7baea0690025abe49eeec3a5d01d` |
| `2.1.314` | 298 MB | `4487042b4c4f5d88d4568f68ae98aae0b1ffae58864cf57fb868641bc5f84594` |
| `2.1.315` | 328 MB | `a3b9d66caf61b40d6aad99ed4de80f48f17e0e024ddf1b6e4289efade06105df` |
| `2.1.316` | 328 MB | `fd13c70000fec3f2116dfbcbb6121210b4a29328bf0e46210a59dd594a67d824` |

This set covers the span the playbook's tree-move measurements were taken over
(`2.1.210` → `2.1.233`), the 2.1.242 split, the 2.1.246 stream-store
migration, the 2.1.247 selector reads it was read back through, the
2.1.257 compiled transcript renderer that retired live thinking's render half,
the 2.1.269 Bun 1.4.3 records that retired the container's record walker, and
the 2.1.248, 2.1.304 and 2.1.314 values moved into other chunks that names now
resolve across, so each is re-checkable here rather than historical.

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
