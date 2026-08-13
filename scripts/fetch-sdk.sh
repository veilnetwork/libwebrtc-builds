#!/usr/bin/env bash
# Download, check and lay out a WebRTC SDK bundle. This is the consumer side.
#
#   fetch-sdk.sh --target linux-x64 --expect-pin <sha> [--into DIR] [--tag TAG]
#
# Prints the two variables the veil_media build scripts need, so a caller can
#
#   eval "$(fetch-sdk.sh --target linux-x64 --expect-pin "$WEBRTC_PIN")"
#   bash .../build_veil_media_so_linux.sh
#
# --expect-pin is the point of this script, not a convenience. The failure to
# design against is a checkout whose wrapper sources expect one WebRTC revision
# and whose bundle came from another: the wrapper calls APIs whose signatures
# move between revisions, so the mismatch does not fail to link, it fails to
# COMPILE, inside a WebRTC header, in a way that reads like veil_media is
# wrong. The manifest is published as its own small asset precisely so this
# check costs one 2 KB request instead of a 70 MB download.
set -euo pipefail

REPO="${VEIL_SDK_REPO:-veilnetwork/libwebrtc-builds}"
TARGET=""; EXPECT=""; INTO="${VEIL_SDK_DIR:-$PWD/.veil-webrtc-sdk}"; TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --target)      TARGET="$2"; shift 2 ;;
    --expect-pin)  EXPECT="$2"; shift 2 ;;
    --into)        INTO="$2";   shift 2 ;;
    --tag)         TAG="$2";    shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$TARGET" ] || { echo "usage: $0 --target NAME --expect-pin SHA [--into DIR]" >&2; exit 2; }
[ -n "$EXPECT" ] || { echo "::error::--expect-pin is required. Fetching a bundle without saying which WebRTC revision the sources expect is how a pin mismatch becomes a compile error in a WebRTC header." >&2; exit 2; }
TAG="${TAG:-webrtc-$EXPECT}"

BASE="https://github.com/$REPO/releases/download/$TAG"
BUNDLE="$INTO/veil-webrtc-sdk-$TARGET"
mkdir -p "$INTO"

# Already laid out and matching? Then this is a no-op, so a second CI step or a
# second local build does not re-download 70 MB.
manifest="$BUNDLE/veil-webrtc-sdk.json"
pin_of() { python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["webrtc_pin"])' "$1"; }

if [ -f "$manifest" ] && [ "$(pin_of "$manifest")" = "$EXPECT" ]; then
  echo "==> $TARGET SDK already present at $BUNDLE" >&2
else
  echo "==> checking the published pin before downloading the bundle" >&2
  probe="$INTO/.manifest-$TARGET.json"
  # No token. Release assets are public, unlike run artifacts, which need
  # credentials AND expire out from under a pinned build.
  curl -fsSL --retry 3 --retry-delay 5 -o "$probe" \
    "$BASE/veil-webrtc-sdk-$TARGET.json" || {
      echo "::error::no manifest for $TARGET at $TAG. Either that target has not been built for this pin, or the release does not exist. See https://github.com/$REPO/releases" >&2
      exit 1; }
  got="$(pin_of "$probe")"
  if [ "$got" != "$EXPECT" ]; then
    echo "::error::the published $TARGET bundle was built from WebRTC $got, but these sources expect $EXPECT." >&2
    echo "         Bump PIN in $REPO and rebuild, or point this at the tag that matches." >&2
    exit 1
  fi
  echo "==> pin matches ($got); downloading" >&2

  case "$TARGET" in
    win-*) ext="zip" ;;
    *)     ext="tar.xz" ;;
  esac
  arc="$INTO/veil-webrtc-sdk-$TARGET.$ext"
  curl -fL --retry 3 --retry-delay 5 -o "$arc" "$BASE/veil-webrtc-sdk-$TARGET.$ext"
  rm -rf "$BUNDLE"
  case "$ext" in
    zip) unzip -q "$arc" -d "$INTO" ;;
    *)   tar -C "$INTO" -xf "$arc" ;;
  esac
  rm -f "$arc"

  # The archive travelled; prove it arrived whole rather than assuming it.
  # A truncated download extracts happily and fails at link time.
  want="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["libwebrtc_sha256"])' "$manifest")"
  name="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["libwebrtc_name"])' "$manifest")"
  out="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["out_dir"])' "$manifest")"
  lib="$BUNDLE/src/$out/obj/$name"
  if command -v sha256sum >/dev/null 2>&1; then
    have="$(sha256sum "$lib" | cut -d' ' -f1)"
  else
    have="$(shasum -a 256 "$lib" | cut -d' ' -f1)"
  fi
  [ "$have" = "$want" ] || {
    echo "::error::$name does not match the manifest checksum — the download is corrupt or substituted" >&2
    exit 1; }
  echo "==> $name verified ($(wc -c < "$lib" | tr -d ' ') bytes)" >&2
fi

bash "$BUNDLE/setup.sh" >&2

echo "export WEBRTC_SRC='$BUNDLE/src'"
echo "export WEBRTC_OUT='$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["out_dir"])' "$manifest")'"
echo "export WEBRTC_BUILD='$BUNDLE'"
