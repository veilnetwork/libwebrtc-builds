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

> Sizes for `linux-x64`, `android-arm64` and `win-x64` are filled in from the
> first published run — they are not the same as macOS, because those bundles
> also carry a sysroot (a Debian bullseye sysroot on Linux, the NDK's aarch64
> sysroot on Android) that macOS gets from local Xcode.

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

---

## Targets

| Target | Host required | Served here |
|---|---|---|
| `linux-x64` | x86-64 Linux | yes, in CI |
| `android-arm64` | x86-64 Linux — WebRTC's `gn` asserts `host_os=="linux"` for Android and the NDK it downloads is `linux-x86_64` only | yes, in CI |
| `win-x64` | Windows | yes, in CI |
| `mac-arm64` | macOS arm64 | by running `scripts/pack_sdk.py` locally, then `gh release upload` |
| `ios-arm64`, `ios-sim-arm64` | macOS arm64 | same — see below |

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
33 GB checkout. With a bundle they need ~350 MB and the `gn`/`ninja` steps are
skipped entirely.

---

## Layout

```
PIN                        the WebRTC + depot_tools revisions. One source of truth.
scripts/pack_sdk.py        cut a bundle from a built WebRTC out/ directory
scripts/verify-sdk.sh      prove a bundle builds the wrapper, checkout hidden
.github/workflows/build.yml
```

`verify-sdk.sh` renames the WebRTC checkout away before building. That is not
belt-and-braces: every include path in the compile command is relative, so a
header the bundle forgot to carry resolves against the original tree and the
test passes for a reason that exists on no other machine. The published bundles
are self-contained **by test**, not by inspection.
