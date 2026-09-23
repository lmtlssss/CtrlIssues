#!/bin/sh
set -eu
VERSION=0.1.1
ROOT=
if [ -f "$0" ]; then
  CANDIDATE=$(CDPATH= cd -- "$(dirname -- "$0")" 2>/dev/null && pwd || true)
  if [ -n "$CANDIDATE" ] && [ -f "$CANDIDATE/scripts/install.py" ] && [ -f "$CANDIDATE/plugins/ctrlissues/.codex-plugin/plugin.json" ]; then ROOT=$CANDIDATE; fi
fi

# An installed checkout keeps the direct local --source/--binary path.
if [ -n "$ROOT" ]; then
  MODE=source
  for ARG in "$@"; do case "$ARG" in --source|--package|--rollback|--uninstall) MODE=passthrough;; esac; done
  if [ "$MODE" = source ]; then exec python3 "$ROOT/scripts/install.py" --source "$ROOT" "$@"; fi
  exec python3 "$ROOT/scripts/install.py" "$@"
fi

OS=$(uname -s)
ARCH=$(uname -m)
case "$OS:$ARCH" in
  Linux:x86_64|Linux:amd64) LABEL=linux-x86_64-musl ;;
  Darwin:arm64|Darwin:aarch64) LABEL=macos-arm64 ;;
  Darwin:x86_64) LABEL=macos-x86_64 ;;
  *) echo "CtrlIssues has no release package for $OS/$ARCH" >&2; exit 2 ;;
esac
ASSET="ctrlissues-v${VERSION}-${LABEL}.zip"
BASE="https://github.com/lmtlssss/CtrlIssues/releases/download/v${VERSION}"
CTRLISSUES_TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/ctrlissues-install.XXXXXX")
trap 'rm -rf "$CTRLISSUES_TMP_DIR"' EXIT HUP INT TERM
if command -v curl >/dev/null 2>&1; then
  curl -fsSL "$BASE/$ASSET.sha256" -o "$CTRLISSUES_TMP_DIR/$ASSET.sha256"
  curl -fsSL "$BASE/$ASSET" -o "$CTRLISSUES_TMP_DIR/$ASSET"
elif command -v wget >/dev/null 2>&1; then
  wget -q "$BASE/$ASSET.sha256" -O "$CTRLISSUES_TMP_DIR/$ASSET.sha256"
  wget -q "$BASE/$ASSET" -O "$CTRLISSUES_TMP_DIR/$ASSET"
else
  echo "Install requires curl or wget" >&2; exit 2
fi
EXPECTED=$(awk 'NR==1 {print $1}' "$CTRLISSUES_TMP_DIR/$ASSET.sha256")
case "$EXPECTED" in *[!0-9a-f]*|'') echo "Invalid release checksum file" >&2; exit 2;; esac
[ "${#EXPECTED}" -eq 64 ] || { echo "Invalid release checksum length" >&2; exit 2; }
if command -v sha256sum >/dev/null 2>&1; then
  ACTUAL=$(sha256sum "$CTRLISSUES_TMP_DIR/$ASSET" | awk '{print $1}')
  [ "$EXPECTED" = "$ACTUAL" ] || { echo "Release checksum mismatch" >&2; exit 1; }
elif command -v shasum >/dev/null 2>&1; then
  ACTUAL=$(shasum -a 256 "$CTRLISSUES_TMP_DIR/$ASSET" | awk '{print $1}')
  [ "$EXPECTED" = "$ACTUAL" ] || { echo "Release checksum mismatch" >&2; exit 1; }
else
  echo "Install requires sha256sum or shasum" >&2; exit 2
fi

# Extract only the bootstrap file by its fixed member name. The bundled
# installer validates every member, identity and hash before payload extraction.
python3 - "$CTRLISSUES_TMP_DIR/$ASSET" "$CTRLISSUES_TMP_DIR/install.py" <<'PY'
import pathlib, stat, sys, zipfile
with zipfile.ZipFile(sys.argv[1]) as z:
    info = z.getinfo("scripts/install.py")
    if info.file_size > 2_000_000 or stat.S_ISLNK(info.external_attr >> 16):
        raise SystemExit("unsafe installer member")
    pathlib.Path(sys.argv[2]).write_bytes(z.read(info))
PY
SOURCE_MODE=no
for ARG in "$@"; do case "$ARG" in --source) SOURCE_MODE=yes;; esac; done
if [ "$SOURCE_MODE" = yes ]; then
  python3 "$CTRLISSUES_TMP_DIR/install.py" "$@"
else
  python3 "$CTRLISSUES_TMP_DIR/install.py" --package "$CTRLISSUES_TMP_DIR/$ASSET" "$@"
fi
