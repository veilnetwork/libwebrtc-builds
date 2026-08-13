# libwebrtc-builds

Prebuilt WebRTC for the `veil_media` call engine, so that building the engine
stops requiring a from-source WebRTC checkout.

---

## Why this is a build repository and not a fork of WebRTC

The instinct is to fork WebRTC and build artifacts in the fork. **WebRTC cannot
usefully be forked**, and it is worth writing down the evidence once so the next
person does not spend a day rediscovering it.

`fetch --nohooks webrtc` does not clone one repository. It writes a `.gclient`
file and then resolves a dependency tree. In the checkout this project builds
from, that tree is:

| | |
|---|---|
| entries `gclient` resolved | **127** |
| separate git repositories checked out | **63** |
| CIPD packages (Google's binary package system) | **53** |
| git hosts involved | `chromium.googlesource.com`, `webrtc.googlesource.com`, `boringssl.googlesource.com`, `aomedia.googlesource.com` |
| binary host | `chrome-infra-packages.appspot.com` |
| post-sync hooks that download more | **23** |

A fork of the `src` repository alone gives you none of that. `src/build`,
`src/buildtools`, `src/third_party`, the clang toolchain, the sysroots and the
NDK all arrive from elsewhere, pinned by `DEPS` inside `src`, and only
`gclient` knows how to assemble them.

And the obvious GitHub mirror does not help. `webrtc-mirror/webrtc` exists and
is public, but as checked on 2026-08-13:

* last push **2026-01-14** — seven months stale;
* exactly one branch (`main`), no `branch-heads/*`;
* the commit this project pins, `4ef980bc2c70834276c791e71e7834b8809f24ad`,
  **is not in it** — the GitHub API answers `422 No commit found for SHA`.

So a fork of the mirror would not even contain the revision being built.

What is actually wanted is a **build repository**: it pins a WebRTC revision,
runs the same `depot_tools` sequence Google's own build uses, and publishes the
result. That is this repository.

---

## What is actually published, and why it is not just `libwebrtc.a`

The repository is named after `libwebrtc.a` and does publish it. But
`libwebrtc.a` **on its own is not enough to build the wrapper**, and shipping
only that would produce a link failure on someone else's machine rather than a
working build.

`veil_media` is not WebRTC. It is a wrapper — an audio device module, camera
capture, playback, a `webrtc::Transport` shim, a video-note recorder — about
eight `.cc`/`.mm` files in `veil/flutter/veil_media/src/`. Its build scripts
compile those files by reading **call.cc's exact command** out of WebRTC's
`compile_commands.json` and swapping only the source file. That command names:

* **the checkout's own clang**, by path
  (`third_party/llvm-build/Release+Asserts/bin/clang++`). It carries flags no
  other clang accepts — `-Xclang -add-plugin -Xclang unsafe-buffers`,
  `-mllvm -split-threshold-for-reg-with-hint=0`,
  `-fsanitize-ignore-for-ubsan-feature=array-bounds`;
* `-I../..` — the WebRTC source tree, for its headers;
* `-nostdinc++ -isystem third_party/libc++/src/include` — **Chromium's own
  libc++**, built into the `__Cr` inline namespace. A wrapper TU compiled
  against any other standard library links cleanly and then crosses an ABI
  boundary at the first `std::string` it passes;
* `--sysroot` / `-isysroot` — the platform sysroot the checkout downloaded;

and the **link** additionally needs the `libc++` / `libc++abi` (and on Android
`libunwind`) *object files* out of the build tree, because the same `__Cr`
namespace has to come from the same build.

So each release carries three things per target:

| Asset | What it is |
|---|---|
| `veil-webrtc-sdk-<target>.tar.xz` | **the one to use.** A pruned checkout: archive + toolchain + headers + sysroot + libc++ objects, at the same relative paths a real checkout has |
| `libwebrtc-<target>.a` | the static library alone — for inspection, or for a consumer that already has a toolchain. Not sufficient by itself |
| `veil-webrtc-sdk-<target>.json` | the manifest, published separately so a pin check does not have to download the bundle |

Windows spells the first two differently, because the platform does:
`veil-webrtc-sdk-win-x64.**zip**` (PowerShell's `Expand-Archive` reads it with no
question, where whether the bundled `tar.exe` was built with liblzma is not
something to discover on a consumer's machine) and `webrtc-win-x64.**lib**`. The
Windows bundle also carries `setup.ps1` beside `setup.sh` — see "Using a bundle".

### Measured sizes

From the `mac-arm64` bundle cut on 2026-08-13 (the one target with an existing
local checkout, so these are measured and not estimated):

| Component | Size | Files |
|---|---|---|
| clang / lld toolchain | 260.6 MiB | 357 |
| headers (WebRTC, abseil, libc++, libyuv, perfetto) | 50.9 MiB | 5 814 |
| **`libwebrtc.a`** | **33.1 MiB** | 1 |
| generated headers (`out/<target>/gen`) | 6.7 MiB | 353 |
| libc++ / libc++abi objects | 1.8 MiB | 66 |
| **total, uncompressed** | **353.2 MiB** | |
| **the published `.tar.xz`** | **66.4 MiB** | |

`libwebrtc.a` itself, measured directly, is **33.1 MiB** on `mac-arm64`
(34 757 200 bytes), **32.1 MiB** on `ios-arm64` and **32.3 MiB** on
`ios-sim-arm64` — built with `is_debug=false symbol_level=0`, which is what
keeps it small. It is a plain `ar` archive of 2 923 members, not a thin one.

GitHub caps a release asset at 2 GB. Nothing here is near it: the largest
component is a ~260 MiB toolchain, and the whole bundle compresses from ~353
MiB. The cap does not shape this design.

Per target, as published in
[`webrtc-4ef980bc…`](https://github.com/veilnetwork/libwebrtc-builds/releases/tag/webrtc-4ef980bc2c70834276c791e71e7834b8809f24ad):

| Target | `libwebrtc.a` | bundle, uncompressed | bundle, published |
|---|---|---|---|
| `mac-arm64` | 33.1 MiB (34 757 200 B) | 353.2 MiB | **66.4 MiB** |
| `ios-arm64` | 32.1 MiB (33 655 768 B) | 352.3 MiB | **66.2 MiB** |
| `linux-x64` | 47.3 MiB (49 588 622 B) | 926.3 MiB | **186.7 MiB** |
| `android-arm64` | 51.4 MiB (53 946 302 B) | 857.6 MiB | **191.9 MiB** |
| `win-x64` | 76.8 MiB (80 484 140 B), as `webrtc.lib` | 411.1 MiB | **158.4 MiB** (`.zip`) |

`win-x64` is the outlier in both columns, in opposite directions. Its library is
the largest — a COFF archive of 2 935 objects, where `mac-arm64`'s is 2 923 in
about half the bytes, because MSVC-mangled names are long and `/Zc:inline` still
leaves more in an unstripped `.lib` than `symbol_level=0` does in a Mach-O `.a`.
Its *bundle* is nonetheless smaller than Linux's, because it carries no sysroot:
Microsoft's headers stay on the consumer's machine, exactly as Apple's do. And
`.zip`/deflate is a weaker compressor than `xz`, so 411 MiB uncompressed lands
at 158 MiB where Linux's 926 MiB reaches 187 MiB.

> The Linux and Android bundles are larger than the Apple ones, because they
> also carry a sysroot — a Debian bullseye sysroot on Linux (166.0 MiB), the
> NDK's aarch64 sysroot on Android (90.9 MiB, pruned to the one architecture)
> — where macOS and iOS take theirs from the Xcode already installed on the
> consumer's machine. Apple's SDK is not ours to redistribute and `setup.sh`
> repoints `-isysroot` at the local one instead.
>
> Their *uncompressed* figures above are also inflated by a packaging bug
> those two bundles were built with: the sysroot lives under `build/`, so it
> was walked for headers and then copied again wholesale — 204.6 MiB of
> `build/` headers on `linux-x64` that were already in the sysroot copy. Fixed
> in `pack_sdk.py` after this run. The published assets are correct, just
> fatter than they need to be; the next pin bump will produce smaller ones.

The largest single component is the clang/lld toolchain: 260.6 MiB on macOS,
274.1 MiB on Windows, 448.9 MiB on Linux. It is bundled rather than fetched from Google because a
second network dependency is a second thing that can be unavailable, and
because 450 MiB next to a 33 GB checkout is not the problem worth solving.

---

## What this buys

Measured on the xVeil `webrtc-linux` workflow, run `31609030118`
(2026-08-12, both jobs green):

| Step | linux-x64 | android-arm64 |
|---|---|---|
| `gclient sync` (the checkout) | 24m 30s | 26m 06s |
| `ninja -C out/… webrtc` | 13m 26s | 15m 36s |
| **the wrapper** (`build_veil_media_so*.sh`) | **18s** | **16s** |
| job total | 40m 13s | 43m 04s |

Roughly forty minutes and ~33 GB of disk produce an input that the actual
wrapper build consumes in under twenty seconds. Splitting them is the entire
point: the wrapper becomes buildable locally, in minutes, by anyone.

This repository's own first run,
[31676947631](https://github.com/veilnetwork/libwebrtc-builds/actions/runs/31676947631),
reproduces those figures and adds what it costs to package:

| Step | linux-x64 | android-arm64 |
|---|---|---|
| `gclient sync` | 25m 59s | 25m 25s |
| `ninja … webrtc` | 12m 41s | 15m 26s |
| package the bundle | 6s | 11s |
| **verify: build the wrapper from the bundle, checkout hidden** | **10s** | **10s** |
| compress | 1m 42s | 1m 35s |

So the cost of publishing is about two minutes on top of a build that had to
happen anyway — and it is paid once per pin instead of once per contributor.

`win-x64`, from run
[31690243280](https://github.com/veilnetwork/libwebrtc-builds/actions/runs/31690243280),
one hour two minutes end to end:

| Step | win-x64 |
|---|---|
| reclaim disk | 5m 03s |
| `depot_tools` clone + bootstrap | 11s |
| `fetch` + `gclient sync` | 30m 47s |
| `gn gen` | 12s |
| `ninja -C out/win-x64 webrtc` | 24m 34s |
| package the bundle | 15s |
| compress (`7z -tzip -mx=6`) | 20s |
| **verify: build `veil_media.dll` from the bundle, checkout hidden** | **20s** |

**Demonstrated**, not projected. On 2026-08-13 the `mac-arm64` bundle was cut
from a local checkout, compressed to the asset that is published, extracted
into an empty directory, and the 33 GB checkout was **renamed out of the way**
before the wrapper was built against the extract alone:

```
==> done: libveil_media.dylib (5.1M)
exported veil_media_* symbols: 87
=== build rc=0 elapsed=9s ===
```

Nine seconds against roughly forty minutes, and xVeil's own
`scripts/check-media-symbols.sh` reports `Mach-O: exported 87, looked up by
Dart: 87 — OK: nothing the app calls is missing from the engine`.

The round trip matters as much as the build: a bundle tested in its staging
directory can pass on file modes and symlinks that a tar does not carry. This
one was tested after `tar | xz` and extraction, and `clang++ -> clang`,
`ld.lld -> lld` and the executable bits all survive.

`win-x64` was proved the same way in CI, and with one extra thing taken away.
`verify-sdk.ps1` renames `C:\webrtc` aside **and clears `INCLUDE`** before the
wrapper build, because `clang-cl` reads that variable for system headers: a
runner already exporting a Visual Studio `INCLUDE` would compile the bundle
whether or not `setup.ps1` had repointed a single path, and pass for a reason
the bundle had no part in. It was `(unset)` in the run, so the nine repointed
`-imsvc` paths are what made the compile work:

```
==> INCLUDE was: (unset)
==> hid C:\webrtc - the bundle is now the only WebRTC on this machine
  -imsvc -> 9 local paths, from C:\Program Files\Microsoft Visual Studio\18\…\include
==> done: veil_media.dll (5.5 MB)
exported veil_media_* symbols: 87
==> VERIFIED: the bundle alone builds the wrapper in 20s
```

Twenty seconds, all eight Windows translation units, all 87 `veil_media_*`
exports — against roughly fifty-five minutes of `sync` plus `ninja`.

---

## Using a bundle

[`scripts/fetch-sdk.sh`](scripts/fetch-sdk.sh) does the whole consumer side —
check the pin, download, verify the archive's checksum, run `setup.sh` — and
prints the variables the wrapper build wants:

```sh
eval "$(curl -fsSL https://raw.githubusercontent.com/veilnetwork/libwebrtc-builds/main/scripts/fetch-sdk.sh \
        | bash -s -- --target linux-x64 --expect-pin 4ef980bc2c70834276c791e71e7834b8809f24ad)"

bash third_party/veil/flutter/veil_media/linux/build_veil_media_so_linux.sh
```

`--expect-pin` is required, not optional: see "Detecting a mismatch" below.

By hand, if you prefer:

```sh
tag=webrtc-4ef980bc2c70834276c791e71e7834b8809f24ad
target=linux-x64

curl -fL -o sdk.tar.xz \
  "https://github.com/veilnetwork/libwebrtc-builds/releases/download/$tag/veil-webrtc-sdk-$target.tar.xz"
tar xf sdk.tar.xz
./veil-webrtc-sdk-$target/setup.sh

WEBRTC_SRC="$PWD/veil-webrtc-sdk-$target/src" WEBRTC_OUT="out/$target" \
  bash third_party/veil/flutter/veil_media/linux/build_veil_media_so_linux.sh
```

`setup.sh` writes `compile_commands.json` with this machine's paths (and, on
macOS/iOS, repoints `-isysroot` at the locally installed Xcode SDK, which is
not ours to redistribute). It is idempotent.

On Windows, run `setup.ps1` instead — it is the same three lines and is not a
convenience. `setup.sh` resolves its own location with bash's `pwd`, which under
Git Bash (the only bash a Windows consumer has) is an MSYS path,
`/c/…/veil-webrtc-sdk-win-x64/src`. `clang-cl` and `lld-link` cannot open those,
so a Windows bundle set up through `setup.sh` writes a `compile_commands.json`
naming a directory the compiler never finds — and it says so as a wall of
missing headers rather than as a bad path. `fetch-sdk.sh` picks the right one
by target.

**`setup.ps1` needs PowerShell 7, not Windows PowerShell 5.1.** It rewrites the
shipped template through a variable property name — `$e.$k = $e.$k.Replace(…)`
on what `ConvertFrom-Json` returned — and 5.1 answers `The property 'directory'
cannot be found on this object` at `setup.ps1:81`. This went unseen because
every green run of it came from `verify-sdk.ps1` inside a `shell: pwsh` step;
the first caller that named a PowerShell was `fetch-sdk.sh`, and it named
`powershell.exe`. Measured on one runner against one bundle, back to back:
5.1.26100.33158 exits 1, pwsh 7.6.4 repoints the nine `-imsvc` paths and writes
`compile_commands.json` (xVeil run
[31714489182](https://github.com/veilnetwork/xVeil/actions/runs/31714489182)).
`fetch-sdk.sh` now prefers `pwsh` and keeps `powershell.exe` only as a fallback.
The durable fix belongs in `pack_sdk.py`'s `SETUP_PS1`, so the bundle itself
works on a machine that has only 5.1 — that changes the *next* bundle, not the
published one, which is why the consumer side is fixed first.

No token is needed. Release assets are public — unlike GitHub *run* artifacts,
which need credentials to download and expire after the retention window. That
expiry is not hypothetical for this project: xVeil's `fetch-deps.py` carries a
whole error path explaining that an engine artifact has expired and cannot be
recovered.

---

## Bumping the pin

`WEBRTC_PIN` lives in exactly one file, [`PIN`](PIN). Every job, script and
manifest reads it from there.

1. Edit `PIN`.
2. Run the `build` workflow. Each pin gets its own release, tagged
   `webrtc-<full sha>`, so old bundles stay downloadable and a consumer pinned
   to the old revision keeps working.
3. Update the consumer's expected pin (see below) — deliberately, because the
   wrapper calls WebRTC APIs whose signatures move between revisions
   (`SendRtp`'s span, `DeliverRtpPacket`'s arity). A different pin does not fail
   to link; it fails to *compile*, later, in a way that reads like the wrapper
   is wrong.

**How often does this have to run?** Once per WebRTC pin, per target. The pin
has moved once since the engine was first built. This is not a workflow that
runs on push, and it deliberately has no `push:` trigger.

---

## Detecting a mismatch before link time

The failure to design against is a consumer whose wrapper sources expect one
WebRTC revision and whose bundle came from another. Left alone, that surfaces
as a compile error deep in a WebRTC header, and it reads like the wrapper is
broken.

The bundle carries `veil-webrtc-sdk.json`:

```json
{
  "schema": 1,
  "target": "mac-arm64",
  "webrtc_pin": "4ef980bc2c70834276c791e71e7834b8809f24ad",
  "depot_tools_pin": "e154c8eda5e63cbe85a765ae9d06e2b7af05139e",
  "clang_revision": "llvmorg-23-init-19482-g53d18800-1",
  "gn_args_sha256": "…",
  "libwebrtc_sha256": "…",
  "libwebrtc_bytes": 34757200
}
```

A consumer compares `webrtc_pin` against the revision its wrapper sources
expect, and `gn_args_sha256` against the argument set it expects, **before**
compiling anything. Both are exact equality checks on values that are cheap to
fetch — the manifest is published as its own small asset for exactly this.

`libwebrtc_sha256` additionally makes a truncated or substituted download
detectable, which a byte count alone does not.

`fetch-sdk.sh` does all of this. Tested against the live release:

| | |
|---|---|
| correct pin | downloads, checksums, sets up — exit 0 |
| wrong `--expect-pin`, real tag | refuses **before** downloading the bundle, naming both revisions — exit 1 |
| no `--expect-pin` at all | refuses — exit 2 |
| correct pin, already present | reuses the extracted bundle, no download — exit 0 |

---

## Targets

| Target | Host required | Served here |
|---|---|---|
| `linux-x64` | x86-64 Linux | yes, in CI |
| `android-arm64` | x86-64 Linux — WebRTC's `gn` asserts `host_os=="linux"` for Android and the NDK it downloads is `linux-x86_64` only | yes, in CI |
| `win-x64` | Windows | yes, in CI |
| `mac-arm64` | macOS arm64 | by running `scripts/pack_sdk.py` locally, then `gh release upload` |
| `ios-arm64`, `ios-sim-arm64` | macOS arm64 | same — see below |

**Why the Windows job bootstraps `depot_tools` by hand.** On Windows,
`depot_tools` does not run `git`. It runs **`git.bat`** — `git_cache.py` opens
with `git_exe = "git.bat" if sys.platform.startswith("win") else "git"` — and
`git.bat` is not in the repository. `.gitignore` lists it, because
`bootstrap/bootstrap.py` *generates* it, as a stub pointed at whatever Git the
machine already has. Nothing else can stand in for it: `CreateProcess` resolves
a name that already carries an extension by exact match along `PATH`, so Git for
Windows' `git.exe` is not a candidate, and `GetCachePath` catches
`CalledProcessError`, not `FileNotFoundError` — so the miss surfaces raw, as
`FileNotFoundError: [WinError 2]`, out of `gclient sync`.

Normally `fetch` triggers that generation on the way past: `fetch.bat` calls
`update_depot_tools.bat`, which ends in `bootstrap\win_tools.bat`. But
`update_depot_tools.bat` returns at its second test —
`IF "%DEPOT_TOOLS_UPDATE%" == "0" GOTO :EOF` — several lines *before* that call.
And this workflow sets `DEPOT_TOOLS_UPDATE=0`, because the lines in between are
`git fetch origin` and `git checkout origin/main`, which would discard
`DEPOT_TOOLS_PIN` on every run.

So **pinning `depot_tools` switches its Windows bootstrap off**, and the price
is only visible six minutes into a sync. That is
[run 31680937721](https://github.com/veilnetwork/libwebrtc-builds/actions/runs/31680937721).
The evidence that it is the pin and not the runner image: xVeil's
`webrtc-windows.yml` built this same target green on 2026-07-31
(run `30665287484`), and at that commit the workflow had **no `DEPOT_TOOLS_PIN`
and no `DEPOT_TOOLS_UPDATE=0`** — so `update_depot_tools.bat` ran all the way
through and generated `git.bat` as a side effect of updating.

The fix is the Windows twin of what the linux job already does with
`ensure_bootstrap`: call `bootstrap\win_tools.bat` explicitly, then assert
`git.bat` exists rather than trusting the exit code. `where git.bat` runs at the
top of the sync step too — resolving the exact name `CreateProcess` will look
for costs a second, where being told by a traceback costs twenty-five minutes.

**What the Windows bundle does not carry.** Microsoft's headers, for the same
reason the Apple bundles do not carry Apple's: they are not ours to
redistribute. Windows names them with `-imsvc` rather than a sysroot, and `gn`
writes those paths *relative to the build directory*, climbing out of the
checkout to reach them:

```
-imsvc../../../../Program Files/Microsoft Visual Studio/18/Enterprise/VC/Tools/MSVC/14.51.36231/include
```

Four levels up from `C:/webrtc/src/out/win-x64` is the drive root, so that
resolves on the machine that built it. Four levels up from
`…/veil-webrtc-sdk-win-x64/src/out/win-x64` is wherever the bundle was
extracted, and `clang-cl` then looks for the Windows SDK inside it. So
`pack_sdk.py` makes every `-imsvc` that resolves outside the checkout absolute,
records them in the manifest as `msvc_includes`, and `setup.ps1` repoints them
at the Visual Studio and Windows SDK installed here — found through `vswhere`
and `Installed Roots`, because the version directories differ per machine.
A Windows consumer therefore needs Visual Studio with the C++ toolchain, the
way a macOS consumer needs Xcode. The link additionally reads `LIB` for
Microsoft's import libraries, which a Developer PowerShell already sets;
`verify-sdk.ps1` builds the same list from `vswhere` when it is unset, which it
was on the runner.

One more thing the Windows bundle does not carry, and neither does the checkout
it was cut from: **`llvm-nm.exe`**. WebRTC's `third_party/llvm-build` ships
`clang-cl`, `lld-link`, `llvm-ml`, `llvm-pdbutil`, `llvm-symbolizer` and
`llvm-undname` on Windows, and no `llvm-nm`. The wrapper build uses it to
generate the export `.def`, and falls back to whatever `llvm-nm.exe` is on
`PATH` — a GitHub runner has one, a developer machine may not. The bundle is no
worse off than a 33 GB checkout here, but it is not better either.

**Why macOS and iOS are packed locally rather than in CI.** The checkout is
~33 GB (measured). The Linux and Windows runners only fit it after an explicit
"Reclaim disk" step that deletes preinstalled SDKs; GitHub-hosted macOS runners
have no comparable headroom to reclaim. Rather than ship a CI job that cannot
work, the same `pack_sdk.py` runs on a Mac that already has a checkout:

```sh
python3 scripts/pack_sdk.py \
  --src ~/Projects/veilnetwork/webrtc-checkout/src \
  --out out/ios-arm64 --target ios-arm64 \
  --dest /tmp/veil-webrtc-sdk-ios-arm64 \
  --webrtc-pin "$(grep ^WEBRTC_PIN PIN | cut -d= -f2)"
tar -C /tmp -cf - veil-webrtc-sdk-ios-arm64 | xz -T0 -6 > sdk.tar.xz
gh release upload webrtc-<sha> sdk.tar.xz
```

This is the larger win for iOS specifically: `build_veil_media_ios.sh` today
runs `gn gen` and `autoninja` itself, so an iOS contributor needs the whole
33 GB checkout. With a bundle they need a 66 MiB download, and the `gn`/`ninja`
steps go away.

Measured, with the checkout renamed away: the eight iOS translation units
compile and archive in **8 seconds**, producing an arm64 `libveil_media.a`
exporting all 87 `veil_media_*` symbols.

For that to be a one-liner rather than the reproduction in `verify-sdk.sh`,
`build_veil_media_ios.sh` needs one guard so its `gn gen` / `autoninja` block
is skipped when `WEBRTC_SRC` already points at a bundle. That edit belongs in
the `veil` repository, not here.

---

## Layout

```
PIN                        the WebRTC + depot_tools revisions. One source of truth.
scripts/pack_sdk.py        cut a bundle from a built WebRTC out/ directory
scripts/verify-sdk.sh      prove a bundle builds the wrapper, checkout hidden
scripts/verify-sdk.ps1     the same proof for win-x64, whose wrapper build is
                           a PowerShell script taking PowerShell parameters
scripts/fetch-sdk.sh       the consumer side: check the pin, download, set up
.github/workflows/build.yml
```

`verify-sdk.sh` renames the WebRTC checkout away before building. That is not
belt-and-braces: every include path in the compile command is relative, so a
header the bundle forgot to carry resolves against the original tree and the
test passes for a reason that exists on no other machine. The published bundles
are self-contained **by test**, not by inspection.
