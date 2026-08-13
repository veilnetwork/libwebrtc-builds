#!/usr/bin/env bash
# Prove a bundle builds the veil_media wrapper — with the WebRTC checkout that
# produced it moved out of the way.
#
#   verify-sdk.sh --bundle <staged dir> --target linux-x64 \
#                 --veil <veil checkout> [--hide <webrtc root>]
#
# The --hide is the whole point and is not optional in CI. A bundle verified
# beside the checkout it was cut from proves nothing: every path in the compile
# command is relative, so a missing header resolves against the original tree
# and the build passes for a reason that will not exist on anyone else's
# machine. Rename the source of truth away and the test becomes real.
#
# The gate on the result is the exported symbol count, with a floor rather than
# an existence check: a wrapper that links but exports nothing is exactly the
# failure that ships an engine the app cannot call into.
set -euo pipefail

BUNDLE=""; TARGET=""; VEIL=""; HIDE=""; DEST=""
while [ $# -gt 0 ]; do
  case "$1" in
    --bundle) BUNDLE="$2"; shift 2 ;;
    --target) TARGET="$2"; shift 2 ;;
    --veil)   VEIL="$2";   shift 2 ;;
    --hide)   HIDE="$2";   shift 2 ;;
    --dest)   DEST="$2";   shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$BUNDLE" ] && [ -n "$TARGET" ] && [ -n "$VEIL" ] \
  || { echo "usage: $0 --bundle DIR --target NAME --veil DIR [--hide DIR]" >&2; exit 2; }

DEST="${DEST:-$(mktemp -d)/engine-out}"
mkdir -p "$DEST"
MEDIA="$VEIL/flutter/veil_media"
[ -d "$MEDIA/src" ] || { echo "no veil_media sources at $MEDIA" >&2; exit 2; }

hidden=""
restore() {
  if [ -n "$hidden" ] && [ -d "$hidden" ]; then
    mv "$hidden" "$HIDE" && echo "restored $HIDE"
  fi
}
trap restore EXIT INT TERM

bash "$BUNDLE/setup.sh"

if [ -n "$HIDE" ] && [ -d "$HIDE" ]; then
  hidden="${HIDE}.hidden-for-verify"
  mv "$HIDE" "$hidden"
  echo "==> hid $HIDE — the bundle is now the only WebRTC on this machine"
  [ -e "$HIDE" ] && { echo "::error::$HIDE still exists after the rename"; exit 1; }
fi

export WEBRTC_SRC="$BUNDLE/src"
export WEBRTC_OUT="out/$TARGET"

start=$(date +%s)
case "$TARGET" in
  linux-*)
    bash "$MEDIA/linux/build_veil_media_so_linux.sh" "$DEST"
    artifact="$DEST/libveil_media.so" ;;
  android-*)
    # This one takes the checkout root rather than src/, and finds out/ itself.
    WEBRTC_BUILD="$(dirname "$BUNDLE/src")" \
      bash "$MEDIA/android/build_veil_media_so.sh" "$DEST"
    artifact="$DEST/libveil_media.so" ;;
  mac-*)
    bash "$MEDIA/macos/build_veil_media_dylib.sh" "$DEST"
    artifact="$DEST/libveil_media.dylib" ;;
  *)
    echo "::error::verify has no recipe for target $TARGET" >&2; exit 2 ;;
esac
elapsed=$(( $(date +%s) - start ))

[ -f "$artifact" ] || { echo "::error::the build reported success and produced no $artifact" >&2; exit 1; }

# Count what the wrapper exports. Two spellings, because the ELF and Mach-O
# tools disagree about both the flag and the leading underscore.
if nm -D --defined-only "$artifact" >/tmp/vsym 2>/dev/null; then
  fmt=ELF
elif nm -gU "$artifact" >/tmp/vsym 2>/dev/null; then
  fmt=Mach-O
else
  echo "::error::cannot read a symbol table out of $artifact — it is not a library" >&2
  exit 1
fi
count=$(awk '{print $NF}' /tmp/vsym | sed 's/^_//' | grep -c '^veil_media_' || true)

echo "==> $fmt $artifact"
echo "==> $(ls -la "$artifact" | awk '{print $5}') bytes, $count exported veil_media_* symbols, built in ${elapsed}s"

# 87 today. A floor, not an equality: adding an entry point to the ABI must not
# turn this red, and dropping half of them must not stay green.
FLOOR=80
if [ "$count" -lt "$FLOOR" ]; then
  echo "::error::only $count veil_media_* symbols exported (floor $FLOOR) — the bundle is incomplete or the engine compiled as its stub"
  exit 1
fi
echo "==> VERIFIED: the bundle alone builds the wrapper in ${elapsed}s"
