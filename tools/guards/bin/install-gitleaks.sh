#!/bin/sh
# Install the pinned gitleaks binary.
# Usage: install-gitleaks.sh [DEST]   DEST defaults to <repo>/.tools/bin/gitleaks;
# a relative DEST is relative to the current directory (make passes $(GITLEAKS)).
# Both pins live in the versioned tools/guards/bin/gitleaks.sha256: the release
# tarball is verified before extraction, and the extracted binary before it is
# copied to DEST. The guards compare DEST against the same binary pin on every
# run (a binary that lies about its version, or was swapped after install,
# fails closed); there is no local install record. The `gitleaks version` text
# is checked here as a third check.
# Exit codes: 0 ok, 2 any failure (unsupported platform, download, checksum).
set -eu

VERSION="8.30.1"
BASE_URL="https://github.com/gitleaks/gitleaks/releases/download/v${VERSION}"

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
root=$(CDPATH= cd -- "${here}/../../.." && pwd)
dest="${1:-${root}/.tools/bin/gitleaks}"
bindir=$(dirname -- "${dest}")
pinned="${here}/gitleaks.sha256"

fail() {
  echo "gitleaks install failed: $1" >&2
  exit 2
}

sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1
  fi
}

# The pinned sha256 of NAME (exactly one line), or fail.
pin_of() {
  n=$(grep -c "^[0-9a-f]\{64\}  $1\$" "${pinned}" || true)
  [ "${n}" = "1" ] || fail "no pinned checksum"
  grep "^[0-9a-f]\{64\}  $1\$" "${pinned}" | cut -d' ' -f1
}

os=$(uname -s)
arch=$(uname -m)
case "${os}-${arch}" in
  Darwin-arm64) plat="darwin_arm64" ;;
  Linux-x86_64) plat="linux_x64" ;;
  *) fail "unsupported platform" ;;
esac

stem="gitleaks_${VERSION}_${plat}"
file="${stem}.tar.gz"
tar_pin=$(pin_of "${file}")
bin_pin=$(pin_of "${stem}/gitleaks")

# Idempotent: nothing to do when the installed binary already has the pinned hash.
if [ -x "${dest}" ] && [ "$(sha256_of "${dest}")" = "${bin_pin}" ]; then
  rm -f "${dest}.sha256"  # legacy local install record, no longer used
  echo "gitleaks ${VERSION} installed"
  exit 0
fi

tmp=$(mktemp -d)
trap 'rm -rf "${tmp}"' EXIT INT TERM

curl -fsSL -o "${tmp}/${file}" "${BASE_URL}/${file}" || fail "download"
[ "$(sha256_of "${tmp}/${file}")" = "${tar_pin}" ] || fail "tarball checksum mismatch"

mkdir -p "${bindir}" "${tmp}/x"
tar -xzf "${tmp}/${file}" -C "${tmp}/x" gitleaks || fail "extract"
[ "$(sha256_of "${tmp}/x/gitleaks")" = "${bin_pin}" ] || fail "binary checksum mismatch"
[ "$("${tmp}/x/gitleaks" version 2>/dev/null || true)" = "${VERSION}" ] || fail "version mismatch"

rm -f "${dest}.sha256"
cp "${tmp}/x/gitleaks" "${dest}"
chmod 0755 "${dest}"
[ "$(sha256_of "${dest}")" = "${bin_pin}" ] || fail "copy mismatch"
echo "gitleaks ${VERSION} installed"
