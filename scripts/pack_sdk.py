#!/usr/bin/env python3
"""Package a built WebRTC out/ directory into a self-contained veil_media SDK.

    pack_sdk.py --src <webrtc>/src --out out/linux-x64 --target linux-x64 \
                --dest /tmp/veil-webrtc-sdk-linux-x64

WHY THIS IS NOT JUST `cp libwebrtc.a`
-------------------------------------
The obvious design — publish `libwebrtc.a` and be done — does not work, and
the reason is worth stating once so nobody re-derives it the expensive way.

veil_media is compiled by reading call.cc's EXACT command out of
`compile_commands.json` and swapping only the source file. That command names:

  * the checkout's own clang (`third_party/llvm-build/.../clang++`) — not a
    system clang. It carries Chromium-only flags (`-Xclang -add-plugin
    unsafe-buffers`, `-mllvm -split-threshold-for-reg-with-hint=0`,
    `-fsanitize-ignore-for-ubsan-feature=...`) that no other clang accepts;
  * `-I../..` — i.e. the WebRTC source tree, for its headers;
  * `-isystem third_party/libc++/src/include` with `-nostdinc++` — Chromium's
    own libc++, built into the `__Cr` inline namespace. A veil TU compiled
    against any other standard library links and then crosses an ABI boundary
    at the first `std::string` it passes;
  * `--sysroot` / `-isysroot` — the platform sysroot the checkout downloaded.

and the LINK additionally needs the `libc++`/`libc++abi` (and on Android
`libunwind`) OBJECT files out of the build tree, because the same `__Cr`
namespace has to come from the same build.

So the unit that makes the wrapper buildable is not one archive. It is the
smallest self-contained slice of the checkout that reproduces that command:
the archive, the toolchain, the headers, the sysroot, and the libc++ objects,
laid out at the SAME relative paths so every `../..` in the command still
resolves. That is what this script cuts, and it is why the asset is a bundle.

The layout is deliberately identical to a real checkout, so the veil build
scripts run against it UNCHANGED:

    <bundle>/src/                       <- stands in for <webrtc>/src
    <bundle>/src/out/<target>/          <- stands in for out/<target>
    <bundle>/setup.sh                   <- writes compile_commands.json
    <bundle>/veil-webrtc-sdk.json       <- the manifest a consumer checks

    WEBRTC_SRC=<bundle>/src WEBRTC_OUT=out/<target> build_veil_media_so_linux.sh
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
import time

# Header-ish files. An allowlist, not a denylist: a denylist that misses one
# extension ships a 26 GB third_party tree, and a denylist that is too broad
# ships a bundle whose first compile fails on a missing include.
HEADER_EXT = {".h", ".hpp", ".hh", ".inc", ".def", ".ipp", ".tcc", ".modulemap"}

# Top-level src directories NOT worth scanning for headers. Everything else is
# scanned, so a WebRTC layout change adds headers rather than silently dropping
# them. third_party is 26 GB and is handled by the explicit list below.
SKIP_TOP = {
    "third_party", "tools", "resources", "examples", "testing", "out",
    ".git", "buildtools", "docs", "infra", "tools_webrtc", "data", ".cipd",
}

# The parts of third_party/ and buildtools/ the compile command actually names.
EXTRA_HEADER_ROOTS = [
    "third_party/abseil-cpp",
    "third_party/libyuv/include",
    "third_party/libc++/src/include",
    "third_party/libc++abi/src/include",
    "third_party/perfetto/include",
    "buildtools/third_party/libc++",
    "buildtools/third_party/libc++abi",
]

# Files named directly by the compile command that are not headers.
EXTRA_FILES = ["unsafe_buffers_paths.txt"]


def log(msg: str) -> None:
    print(msg, flush=True)


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024.0
    return str(n)


def is_header(name: str) -> bool:
    root, ext = os.path.splitext(name)
    if ext.lower() in HEADER_EXT:
        return True
    # libc++ ships <vector>, <string> and friends with no extension at all, and
    # abseil has a few. Dotless files under a header root are headers.
    return ext == "" and not name.startswith(".")


class Copier:
    """Copies into the staging tree, counting bytes so the log says what the
    bundle is made of. A bundle nobody can account for is a bundle nobody
    trusts the size of."""

    def __init__(self, src_root: str, stage_src: str):
        self.src_root = os.path.abspath(src_root)
        self.stage_src = stage_src
        self.tally: dict[str, list[int]] = {}

    def _account(self, bucket: str, nbytes: int, nfiles: int = 1) -> None:
        t = self.tally.setdefault(bucket, [0, 0])
        t[0] += nfiles
        t[1] += nbytes

    def copy_file(self, rel: str, bucket: str) -> bool:
        src = os.path.join(self.src_root, rel)
        if not os.path.isfile(src):
            return False
        dst = os.path.join(self.stage_src, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst, follow_symlinks=True)
        self._account(bucket, os.path.getsize(src))
        return True

    def copy_headers(self, rel_root: str, bucket: str,
                     skip_abs: "set[str] | None" = None) -> None:
        abs_root = os.path.join(self.src_root, rel_root)
        if not os.path.isdir(abs_root):
            log(f"    (absent: {rel_root})")
            return
        skip_abs = skip_abs or set()
        n = 0
        nbytes = 0
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d not in (".git", "out")]
            # The sysroot lives under build/ and is copied WHOLE further down.
            # Walking it for headers first copied 23 749 files / 204.6 MiB on
            # linux-x64 that the sysroot pass then wrote again — the bundle was
            # carrying its own sysroot headers twice.
            dirnames[:] = [d for d in dirnames
                           if os.path.join(dirpath, d) not in skip_abs]
            for fn in filenames:
                if not is_header(fn):
                    continue
                src = os.path.join(dirpath, fn)
                if os.path.islink(src) and not os.path.exists(src):
                    continue
                rel = os.path.relpath(src, self.src_root)
                dst = os.path.join(self.stage_src, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                try:
                    shutil.copy2(src, dst, follow_symlinks=True)
                except OSError:
                    continue
                n += 1
                nbytes += os.path.getsize(dst)
        self._account(bucket, nbytes, n)
        log(f"    {rel_root}: {n} files, {human(nbytes)}")

    def copy_tree(self, rel_root: str, bucket: str,
                  keep: "callable | None" = None) -> None:
        abs_root = os.path.join(self.src_root, rel_root)
        if not os.path.isdir(abs_root):
            log(f"    (absent: {rel_root})")
            return
        n = 0
        nbytes = 0
        for dirpath, dirnames, filenames in os.walk(abs_root):
            dirnames[:] = [d for d in dirnames if d != ".git"]
            for fn in filenames:
                src = os.path.join(dirpath, fn)
                rel = os.path.relpath(src, self.src_root)
                if keep and not keep(rel):
                    continue
                dst = os.path.join(self.stage_src, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.islink(src):
                    # A dangling symlink in a sysroot is normal (absolute links
                    # into a root that is not mounted). Copy the link itself.
                    target = os.readlink(src)
                    if os.path.lexists(dst):
                        os.remove(dst)
                    os.symlink(target, dst)
                    n += 1
                    continue
                try:
                    shutil.copy2(src, dst, follow_symlinks=False)
                except OSError:
                    continue
                # Preserve the executable bit; clang and lld are useless without it.
                try:
                    st = os.stat(src)
                    if st.st_mode & stat.S_IXUSR:
                        os.chmod(dst, os.stat(dst).st_mode | 0o111)
                    nbytes += st.st_size
                except OSError:
                    pass
                n += 1
        self._account(bucket, nbytes, n)
        log(f"    {rel_root}: {n} files, {human(nbytes)}")


def load_template(cc_json: str) -> dict:
    with open(cc_json) as fh:
        entries = json.load(fh)
    for e in entries:
        if e.get("file", "").replace("\\", "/").endswith("call/call.cc"):
            return e
    raise SystemExit(f"call/call.cc is not in {cc_json}")


def command_of(entry: dict) -> str:
    if entry.get("command"):
        return entry["command"]
    return " ".join(entry["arguments"])


def find_sysroot(cmd: str) -> str | None:
    m = re.search(r"--sysroot=(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.search(r"-isysroot\s+(\S+)", cmd)
    if m:
        return m.group(1)
    m = re.search(r"/winsysroot\s*(\S+)", cmd)
    if m:
        return m.group(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="the WebRTC src/ directory")
    ap.add_argument("--out", required=True, help="build dir relative to src, e.g. out/linux-x64")
    ap.add_argument("--target", required=True)
    ap.add_argument("--dest", required=True, help="staging directory to create")
    ap.add_argument("--webrtc-pin", required=True)
    ap.add_argument("--depot-tools-pin", default="")
    ap.add_argument("--schema", default="1")
    ap.add_argument("--built-by", default="")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    outdir = os.path.join(src, args.out)
    cc_json = os.path.join(outdir, "compile_commands.json")
    if not os.path.isfile(cc_json):
        raise SystemExit(f"no {cc_json} — gn gen must run with --export-compile-commands")

    windows = args.target.startswith("win")
    android = "android" in args.target
    apple = args.target.startswith(("mac", "ios"))

    lib_rel = os.path.join(args.out, "obj", "webrtc.lib" if windows else "libwebrtc.a")
    lib_abs = os.path.join(src, lib_rel)
    if not os.path.isfile(lib_abs):
        raise SystemExit(f"no {lib_abs} — run `ninja -C {args.out} webrtc` first")

    entry = load_template(cc_json)
    cmd = command_of(entry)

    stage = os.path.abspath(args.dest)
    if os.path.exists(stage):
        shutil.rmtree(stage)
    stage_src = os.path.join(stage, "src")
    os.makedirs(stage_src)
    cp = Copier(src, stage_src)

    # Locate the sysroot BEFORE walking for headers. It lives under build/ on
    # Linux and under third_party/ on Android, and it is copied whole further
    # down — so the header walk has to be told to leave it alone or the bundle
    # carries every sysroot header twice.
    sysroot_rel = find_sysroot(cmd)
    sysroot_abs = ""
    sysroot_note = "none (host toolchain)"
    if sysroot_rel:
        candidate = os.path.normpath(os.path.join(entry["directory"], sysroot_rel))
        # realpath, not the literal path: on macOS `-isysroot` points at
        # out/<t>/sdk/xcode_links/MacOSX*.sdk, which LOOKS like it is inside the
        # checkout and is a symlink into Xcode. Following it would copy several
        # gigabytes of Apple SDK into a bundle that must not carry it.
        inside = (os.path.realpath(candidate)
                  .startswith(os.path.realpath(src) + os.sep))
        if inside and os.path.isdir(candidate) and not os.path.islink(candidate):
            sysroot_abs = candidate

    log("==> headers from the WebRTC tree")
    skip = {sysroot_abs} if sysroot_abs else set()
    for name in sorted(os.listdir(src)):
        if name in SKIP_TOP:
            continue
        if os.path.isdir(os.path.join(src, name)):
            cp.copy_headers(name, "headers", skip_abs=skip)
    log("==> headers from the named third_party roots")
    for root in EXTRA_HEADER_ROOTS:
        cp.copy_headers(root, "headers", skip_abs=skip)
    for extra in EXTRA_FILES:
        if cp.copy_file(extra, "headers"):
            log(f"    {extra}")

    log("==> the checkout's own clang/lld (the command names it by path)")
    cp.copy_tree("third_party/llvm-build/Release+Asserts", "toolchain")

    log("==> the static library")
    cp.copy_file(lib_rel, "libwebrtc")
    log(f"    {lib_rel}: {human(os.path.getsize(lib_abs))}")

    log("==> Chromium libc++ / libc++abi / libunwind objects (the __Cr namespace)")
    objext = ".obj" if windows else ".o"
    for comp in ("libc++", "libc++abi", "libunwind"):
        rel = os.path.join(args.out, "obj", "buildtools", "third_party", comp)
        cp.copy_tree(rel, "cxx-objects",
                     keep=lambda r, e=objext: r.endswith(e) or r.endswith(".a"))

    log("==> generated headers")
    cp.copy_tree(os.path.join(args.out, "gen"), "gen",
                 keep=lambda r: is_header(os.path.basename(r)))
    cp.copy_file(os.path.join(args.out, "args.gn"), "gen")

    if sysroot_rel:
        if sysroot_abs:
            rel_root = os.path.relpath(sysroot_abs, src)
            log(f"==> sysroot {rel_root}")
            if android:
                # The NDK sysroot carries every architecture and every API
                # level. The bundle is one target; shipping the rest is a
                # few hundred megabytes of libraries nothing links.
                def keep_android(r: str) -> bool:
                    p = r.replace("\\", "/")
                    if "/usr/lib/" in p:
                        return "/usr/lib/aarch64-linux-android" in p
                    return True
                cp.copy_tree(rel_root, "sysroot", keep=keep_android)
            else:
                cp.copy_tree(rel_root, "sysroot")
            sysroot_note = rel_root
        else:
            # macOS and iOS: -isysroot points into Xcode, which is not ours to
            # redistribute. setup.sh rewrites it to the consumer's own SDK.
            sysroot_note = f"external: {sysroot_rel}"
            log(f"==> sysroot is outside the checkout ({sysroot_rel}) — setup.sh will "
                f"point it at the local SDK")

    # `-MMD -MF obj/call/call/call.o.d` survives into every veil TU, because the
    # build scripts rewrite the source and `-o` and nothing else. clang creates
    # the .d file but NOT its directory, so without this the first compile in a
    # fresh bundle dies on a path that has nothing to do with the wrapper.
    os.makedirs(os.path.join(stage_src, args.out, "obj", "call", "call"), exist_ok=True)

    # A one-entry compile_commands.json with every absolute path replaced by a
    # token. setup.sh substitutes the real extraction path back in.
    token = "@VEIL_SDK_SRC@"
    tmpl = {
        "directory": token + "/" + args.out.replace("\\", "/"),
        "command": cmd.replace(src, token).replace(src.replace("\\", "/"), token),
        "file": token + "/call/call.cc",
    }
    with open(os.path.join(stage, "compile_commands.json.in"), "w") as fh:
        json.dump([tmpl], fh, indent=1)

    sha = hashlib.sha256()
    with open(lib_abs, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            sha.update(chunk)

    args_gn = ""
    args_gn_path = os.path.join(outdir, "args.gn")
    if os.path.isfile(args_gn_path):
        with open(args_gn_path) as fh:
            args_gn = fh.read()
    args_gn_norm = " ".join(sorted(x for x in args_gn.split("\n") if x.strip()))

    clang_rev = ""
    m = re.search(r'CR_CLANG_REVISION=\\?"([^"\\]+)', cmd)
    if m:
        clang_rev = m.group(1)

    total = sum(v[1] for v in cp.tally.values())
    manifest = {
        "schema": int(args.schema),
        "target": args.target,
        "webrtc_pin": args.webrtc_pin,
        "depot_tools_pin": args.depot_tools_pin,
        "clang_revision": clang_rev,
        "gn_args": args_gn_norm,
        "gn_args_sha256": hashlib.sha256(args_gn_norm.encode()).hexdigest(),
        "libwebrtc_name": os.path.basename(lib_abs),
        "libwebrtc_bytes": os.path.getsize(lib_abs),
        "libwebrtc_sha256": sha.hexdigest(),
        "out_dir": args.out.replace("\\", "/"),
        "sysroot": sysroot_note,
        "bundle_bytes_uncompressed": total,
        "components": {k: {"files": v[0], "bytes": v[1]} for k, v in sorted(cp.tally.items())},
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "built_by": args.built_by,
    }
    with open(os.path.join(stage, "veil-webrtc-sdk.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)
        fh.write("\n")

    write_setup(stage, args.target, args.out, apple)

    log("")
    log("==> bundle composition")
    for k, (n, b) in sorted(cp.tally.items(), key=lambda kv: -kv[1][1]):
        log(f"    {human(b):>12}  {n:6d} files  {k}")
    log(f"    {human(total):>12}  TOTAL uncompressed")
    log(f"==> staged {stage}")
    return 0


SETUP = r'''#!/usr/bin/env bash
# Point this bundle at the machine it was extracted onto. Run once, after
# extracting; it is idempotent.
#
# All it does is write compile_commands.json from the shipped template, with
# the extraction path substituted for the token. The veil build scripts read
# call.cc's command out of that file, so this is the whole of "installing".
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET='__TARGET__'
OUTDIR='__OUTDIR__'
APPLE='__APPLE__'

python3 - "$ROOT" "$OUTDIR" "$TARGET" "$APPLE" <<'PY'
import json, os, re, subprocess, sys
root, outdir, target, apple = sys.argv[1:5]
src = os.path.join(root, "src")
with open(os.path.join(root, "compile_commands.json.in")) as fh:
    entries = json.load(fh)
for e in entries:
    for k in ("directory", "command", "file"):
        e[k] = e[k].replace("@VEIL_SDK_SRC@", src)
    if apple == "1":
        # -isysroot named the SDK of the machine that BUILT this. Apple's SDK
        # is not ours to redistribute and the consumer's version differs, so
        # the flag is rewritten to whatever Xcode is installed here. Everything
        # else about the command is left exactly as WebRTC compiled with it.
        sdk = {"ios": "iphoneos", "ios-sim": "iphonesimulator"}.get(target, "macosx")
        if target.startswith("ios") and "sim" in target:
            sdk = "iphonesimulator"
        elif target.startswith("ios"):
            sdk = "iphoneos"
        path = subprocess.run(["xcrun", "--sdk", sdk, "--show-sdk-path"],
                              capture_output=True, text=True, check=True).stdout.strip()
        e["command"] = re.sub(r"-isysroot\s+\S+", "-isysroot " + path, e["command"])
        print("  -isysroot ->", path)
        # The macOS/iOS LINK step does not read compile_commands.json; it does
        # `ls sdk/xcode_links | grep MacOSX*.sdk` inside the out dir and passes
        # that to -isysroot itself. So the link needs the same symlink the real
        # checkout has, recreated against the Xcode installed here.
        links = os.path.join(src, outdir, "sdk", "xcode_links")
        os.makedirs(links, exist_ok=True)
        link = os.path.join(links, os.path.basename(path))
        if os.path.lexists(link):
            os.remove(link)
        os.symlink(path, link)
        print("  sdk/xcode_links/" + os.path.basename(path), "->", path)
dest = os.path.join(src, outdir, "compile_commands.json")
os.makedirs(os.path.dirname(dest), exist_ok=True)
with open(dest, "w") as fh:
    json.dump(entries, fh, indent=1)
print("  wrote", dest)
PY

cat <<EOF

Ready. Build the veil_media wrapper against this bundle with:

  WEBRTC_SRC=$ROOT/src WEBRTC_OUT=$OUTDIR \\
    <veil>/flutter/veil_media/<platform>/build_veil_media_*.sh

EOF
'''


def write_setup(stage: str, target: str, outdir: str, apple: bool) -> None:
    body = (SETUP
            .replace("__TARGET__", target)
            .replace("__OUTDIR__", outdir.replace("\\", "/"))
            .replace("__APPLE__", "1" if apple else "0"))
    path = os.path.join(stage, "setup.sh")
    with open(path, "w") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


if __name__ == "__main__":
    sys.exit(main())
