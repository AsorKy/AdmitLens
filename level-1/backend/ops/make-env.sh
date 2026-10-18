#!/bin/sh
# Create the local .env from .env.example (ADR-0010 §2.5). Called by `make env`.
#
# Usage: make-env.sh [EXAMPLE [TARGET]]   (defaults: .env.example .env)
#
# * Refuses when TARGET exists (file or symlink) and leaves it untouched.
# * Each `NAME=__GENERATE__` gets `openssl rand -hex 24` (48 hex characters),
#   captured by the shell and written with the printf builtin: a value is never
#   an argument of an external command, so it never shows up in `ps`.
# * `${NAME}` inside a value is replaced by the value of NAME defined above it,
#   so the database URLs are composed here, inside the shell.
# * Writes a mode-600 temporary file next to TARGET and links it into place
#   (`ln` never overwrites), so a failure never leaves a partial .env.
# * Prints variable NAMES only, never values.
# * The database volume keeps the password of its FIRST init: a new .env over an
#   existing volume does not change it. Both outcomes print that reminder.
# Exit: 0 written, 1 TARGET exists, 2 invalid example, missing tool or unusable filesystem.
set -eu
# Byte semantics for every case pattern and grep: a UTF-8 locale must not widen [A-Z].
LC_ALL=C
export LC_ALL
umask 077

example=${1:-.env.example}
target=${2:-.env}
tmp=

VOLUME_HINT="if the level-1 database volume already exists, its password was fixed at first init: run make db-down first"
CR=$(printf '\r')

fail() {
  echo "make env: $1" >&2
  exit "${2:-2}"
}

cleanup() {
  if [ -n "${tmp}" ]; then rm -f "${tmp}"; fi
}
trap cleanup EXIT
trap 'exit 2' HUP INT TERM

if [ -e "${target}" ] || [ -L "${target}" ]; then
  echo "make env: note: ${VOLUME_HINT}" >&2
  fail "${target} already exists; not touching it (delete it yourself to start over)" 1
fi
[ -f "${example}" ] || fail "${example} not found"
command -v openssl >/dev/null 2>&1 || fail "openssl is required"

tmp=$(mktemp "${target}.tmp.XXXXXX") || fail "cannot create a temporary file next to ${target}"
chmod 600 "${tmp}"

names=
generated=

# expand VALUE: sets `expanded` to VALUE with every ${NAME} replaced. Names are
# validated before eval, and eval only ever sees a variable name, never a value.
expand() {
  rest=$1
  expanded=
  while :; do
    case ${rest} in
      (*'${'*'}'*)
        pre=${rest%%'${'*}
        after=${rest#*'${'}
        ref=${after%%'}'*}
        rest=${after#*'}'}
        case ${ref} in
          ('' | [0-9]* | *[!A-Za-z0-9_]*) fail "invalid reference in the value of ${name}" ;;
        esac
        eval "isset=\${ev_${ref}+x}"
        [ -n "${isset}" ] || fail "${name} refers to ${ref}, which is not defined above it"
        eval "refval=\${ev_${ref}}"
        expanded=${expanded}${pre}${refval}
        ;;
      (*)
        expanded=${expanded}${rest}
        break
        ;;
    esac
  done
}

lineno=0
while IFS= read -r line || [ -n "${line}" ]; do
  lineno=$((lineno + 1))
  case ${line} in
    ('' | '#'*)
      printf '%s\n' "${line}" >>"${tmp}"
      continue
      ;;
    (*=*) ;;
    (*) fail "${example}:${lineno}: expected NAME=VALUE" ;;
  esac
  name=${line%%=*}
  value=${line#*=}
  case ${name} in
    ('' | [0-9]* | *[!A-Z0-9_]*) fail "${example}:${lineno}: invalid variable name" ;;
  esac
  case " ${names} " in
    (*" ${name} "*) fail "${example}:${lineno}: ${name} is defined twice" ;;
  esac

  if [ "${value}" = "__GENERATE__" ]; then
    value=$(openssl rand -hex 24) || fail "openssl rand failed for ${name}"
    case ${value} in
      (*[!0-9a-f]*) fail "openssl rand returned an unexpected format for ${name}" ;;
    esac
    [ "${#value}" -eq 48 ] || fail "openssl rand returned an unexpected length for ${name}"
    generated="${generated} ${name}"
  else
    expand "${value}"
    value=${expanded}
  fi

  # A leftover placeholder or a CR (a CRLF example) would be a broken credential.
  case ${value} in
    (*__GENERATE* | *"${CR}"*) fail "${example}:${lineno}: ${name} has a placeholder or carriage return left" ;;
  esac

  eval "ev_${name}=\${value}"
  names="${names} ${name}"
  printf '%s=%s\n' "${name}" "${value}" >>"${tmp}"
done <"${example}"

[ -n "${names}" ] || fail "${example} defines no variables"

# ln fails when the target appeared meanwhile, so an existing .env is never replaced.
if ! ln "${tmp}" "${target}" 2>/dev/null; then
  if [ -e "${target}" ] || [ -L "${target}" ]; then
    fail "${target} appeared while writing; not touching it" 1
  fi
  fail "cannot link ${target} (filesystem without hard links?)"
fi
rm -f "${tmp}"
tmp=

echo "make env: wrote ${target} (mode 600) with:${names}"
if [ -n "${generated}" ]; then
  echo "make env: generated:${generated}"
fi
echo "make env: note: ${VOLUME_HINT}"
