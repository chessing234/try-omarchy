#!/bin/bash

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage: macos/package-dmg.sh [--sign-identity IDENTITY]
                            [--notarize-profile PROFILE]
                            APP [OUTPUT_DMG]
EOF
  exit 64
}

fail() {
  echo "package-dmg: $*" >&2
  exit 1
}

sign_identity=
notarize_profile=
while (($#)); do
  case "$1" in
    --sign-identity)
      (($# >= 2)) || usage
      sign_identity=$2
      shift 2
      ;;
    --notarize-profile)
      (($# >= 2)) || usage
      notarize_profile=$2
      shift 2
      ;;
    --) shift; break ;;
    -*) usage ;;
    *) break ;;
  esac
done
(( $# == 1 || $# == 2 )) || usage

if [[ -n $notarize_profile && ( -z $sign_identity || $sign_identity == - ) ]]; then
  fail "notarization requires --sign-identity with a Developer ID Application identity"
fi

app=$1
[[ $app == /* && -d $app && ! -L $app && $app == *.app ]] || \
  fail "APP must be an absolute direct .app bundle"
output=${2:-"${app%.app}.dmg"}
[[ $output == /* && $output == *.dmg ]] || fail "OUTPUT_DMG must be an absolute .dmg path"
[[ ! -e $output && ! -L $output ]] || fail "output already exists: $output"

for tool in codesign diskutil ditto hdiutil ln mkdir mktemp osascript python3 rm sync; do
  command -v "$tool" >/dev/null 2>&1 || fail "required tool is unavailable: $tool"
done
codesign --verify --deep --strict --verbose=2 "$app"

native_dir=$(cd "$(dirname "$0")" && pwd -P)
layout_source="$native_dir/dmg-layout.applescript"
[[ -f $layout_source && ! -L $layout_source ]] || {
  fail "DMG Finder layout is missing or unsafe"
}

work_dir=$(mktemp -d /private/tmp/omarchy-dmg.XXXXXX)
publish_dir=
mounted=0
working_volume_name="Try Omarchy ${work_dir##*/}"
mount_dir="$work_dir/$working_volume_name"
cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if (( mounted )); then
    hdiutil detach "$mount_dir" -force >/dev/null 2>&1 || true
  fi
  [[ -z $publish_dir ]] || rm -rf -- "$publish_dir"
  rm -rf -- "$work_dir"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n $notarize_profile ]]; then
  command -v xcrun >/dev/null 2>&1 || fail "xcrun is required for notarization"
fi

publish_dir=$(mktemp -d "${output%/*}/.omarchy-dmg.XXXXXX")
packaged_dmg="$publish_dir/${output##*/}"
staging="$work_dir/dmg"
mkdir -p "$staging"
ditto "$app" "$staging/${app##*/}"
ln -s /Applications "$staging/Applications"

read_write_dmg="$work_dir/Omarchy-rw.dmg"
hdiutil create \
  -volname "$working_volume_name" \
  -srcfolder "$staging" \
  -fs APFS \
  -format UDRW \
  "$read_write_dmg" >/dev/null

mkdir "$mount_dir"
hdiutil attach \
  -readwrite \
  -noverify \
  -noautoopen \
  -mountpoint "$mount_dir" \
  "$read_write_dmg" >/dev/null
mounted=1

osascript "$layout_source" "$mount_dir" "${app##*/}"

# Finder references can use volume and window names. Keep both the volume
# label and mount folder distinct until Finder has finished arranging it.
diskutil rename "$mount_dir" "Try Omarchy" >/dev/null
sync
hdiutil detach "$mount_dir" >/dev/null
mounted=0
hdiutil convert \
  "$read_write_dmg" \
  -format UDZO \
  -imagekey zlib-level=9 \
  -o "$packaged_dmg" >/dev/null

if [[ -n $sign_identity ]]; then
  codesign \
    --sign "$sign_identity" \
    --identifier dev.tryomarchy.native.disk-image \
    --timestamp \
    "$packaged_dmg"
  codesign --verify --strict --verbose=2 "$packaged_dmg"
fi

if [[ -n $notarize_profile ]]; then
  xcrun notarytool submit "$packaged_dmg" --keychain-profile "$notarize_profile" --wait
  xcrun stapler staple "$app"
  xcrun stapler validate "$app"
  xcrun stapler staple "$packaged_dmg"
  xcrun stapler validate "$packaged_dmg"
fi

python3 -I - "$packaged_dmg" "$output" <<'PY'
import ctypes
import os
import sys

source, target = sys.argv[1:]
RENAME_EXCL = 0x00000004
renamex_np = ctypes.CDLL(None, use_errno=True).renamex_np
renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
renamex_np.restype = ctypes.c_int
if renamex_np(os.fsencode(source), os.fsencode(target), RENAME_EXCL) != 0:
    error = ctypes.get_errno()
    print(f"package-dmg: cannot publish {target}: {os.strerror(error)}", file=sys.stderr)
    raise SystemExit(1)
PY

echo "[native] Packaged $output"
