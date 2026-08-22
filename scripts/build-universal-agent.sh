#!/usr/bin/env bash
# Builds the universal2 (arm64 + x86_64) native agent released with the
# macos-harness wheel, and places it at the exact path the wheel bundles:
# src/macos_harness/bin/macos-harness-agent.
#
# Usage: scripts/build-universal-agent.sh
#
# Requires the Xcode Command Line Tools (swift, lipo, codesign) on macOS.
# Builds each architecture as its own release slice via `swift build
# --triple`, in a scratch directory dedicated to this script (never
# native/macos-harness-agent/.build, which a developer's own `swift
# build`/`swift test` may be using concurrently), then combines the two
# slices with a single `lipo -create`. The combined binary is freshly
# ad-hoc signed after combining -- lipo does not carry a valid signature
# across the merge -- and both architectures are verified present before
# signing, and the signature is verified again after.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PACKAGE_DIR="$ROOT/native/macos-harness-agent"
PRODUCT="macos-harness-agent"
DEST="$ROOT/src/macos_harness/bin/$PRODUCT"

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "build-universal-agent.sh: requires macOS (found $(uname -s))" >&2
  exit 1
fi

for tool in swift lipo codesign; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "build-universal-agent.sh: missing required tool '$tool' (install the Xcode Command Line Tools)" >&2
    exit 1
  fi
done

SCRATCH_DIR="$(mktemp -d "${TMPDIR:-/tmp}/macos-harness-universal-build.XXXXXX")"
trap 'rm -rf "$SCRATCH_DIR"' EXIT

has_both_archs() {
  local archs
  archs="$(lipo -archs "$1" 2>/dev/null)" || return 1
  [[ " $archs " == *" arm64 "* && " $archs " == *" x86_64 "* ]]
}

slice_paths=()
for triple in arm64-apple-macosx x86_64-apple-macosx; do
  echo "build-universal-agent.sh: building release slice for $triple" >&2
  swift build \
    --package-path "$PACKAGE_DIR" \
    --configuration release \
    --triple "$triple" \
    --scratch-path "$SCRATCH_DIR" \
    --product "$PRODUCT" >&2
  slice_path="$SCRATCH_DIR/$triple/release/$PRODUCT"
  [[ -f "$slice_path" ]] || {
    echo "build-universal-agent.sh: expected build output missing: $slice_path" >&2
    exit 1
  }
  slice_paths+=("$slice_path")
done

mkdir -p "$(dirname "$DEST")"
rm -f "$DEST"
lipo -create -output "$DEST" "${slice_paths[@]}"
chmod 755 "$DEST"

has_both_archs "$DEST" || {
  echo "build-universal-agent.sh: final binary is missing an architecture (got: $(lipo -archs "$DEST" 2>/dev/null || echo unreadable))" >&2
  exit 1
}

codesign --force --sign - "$DEST"
codesign --verify --strict "$DEST"

echo "build-universal-agent.sh: built universal2 agent ($(lipo -archs "$DEST")) at ${DEST#"$ROOT"/}"
