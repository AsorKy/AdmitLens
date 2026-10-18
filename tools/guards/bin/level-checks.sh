#!/bin/sh
# Quality gate run by `make hook-pre-push` AFTER the leak guards passed:
# `make check` in every level-N/backend that the pushed range changes.
# Usage: level-checks.sh REMOTE REFS_FILE
#   REFS_FILE holds git's pre-push stdin ("<local ref> <local sha> <remote ref> <remote sha>").
#   LEVEL_MAKE (env) is the make binary to use; defaults to `make`.
# A level whose Makefile defines no `check` target only gets a warning: this is a
# quality gate, not a leak guard. Exit: 0 ok or nothing to check, non-zero if a check fails.
set -eu
# Note: case patterns use the leading "(" form; macOS /bin/sh (bash 3.2) breaks on
# a bare "pattern)" inside $( ... ).

remote=$1
refs=$2
mk=${LEVEL_MAKE:-make}

changed=$(
  while read -r _lref lsha _rref rsha; do
    [ -n "${lsha:-}" ] || continue
    case "${lsha}" in (*[!0]*) ;; (*) continue ;; esac # branch deletion: nothing to check
    case "${rsha:-0}" in
      (*[!0]*) git diff --name-only "${rsha}" "${lsha}" -- ;;
      (*) git log -m --format= --name-only "${lsha}" --not --remotes="${remote}" -- ;;
    esac
  done <"${refs}" | sed -n 's#^\(level-[0-9][0-9]*/backend\)/.*#\1#p' | sort -u
)

[ -n "${changed}" ] || exit 0

for dir in ${changed}; do
  if [ -f "${dir}/Makefile" ] && "${mk}" -s -n -C "${dir}" check >/dev/null 2>&1; then
    echo "pre-push: make -C ${dir} check"
    "${mk}" -s -C "${dir}" check
  else
    echo "warn  ${dir}: no check target in its Makefile; quality gate skipped" >&2
  fi
done
