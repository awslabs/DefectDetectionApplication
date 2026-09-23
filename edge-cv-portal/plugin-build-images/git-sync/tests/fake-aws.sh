#!/usr/bin/env bash
# Offline stand-in for the AWS CLI used by the git-sync runner tests.
# Maps s3://bucket/prefix to $FAKE_S3_ROOT/bucket/prefix on the local disk.
#
#   aws s3 sync SRC DST [--delete] [--exact-timestamps] [--quiet]
#   aws s3 cp   SRC DST [--quiet]
set -eu

root="${FAKE_S3_ROOT:?FAKE_S3_ROOT must point at the fake bucket root}"

map() {
  case "$1" in
    s3://*) printf '%s' "$root/${1#s3://}" ;;
    *) printf '%s' "$1" ;;
  esac
}

cmd="${1:-} ${2:-}"
case "$cmd" in
  "s3 sync")
    src="$(map "$3")"; dst="$(map "$4")"; shift 4
    delete=0
    for flag in "$@"; do [ "$flag" = "--delete" ] && delete=1; done
    if [ ! -d "$src" ]; then
      echo "fake-aws: source $src does not exist" >&2
      exit 1
    fi
    if [ "$delete" = 1 ]; then rm -rf "$dst"; fi
    mkdir -p "$dst"
    cp -a "$src/." "$dst/"
    ;;
  "s3 cp")
    src="$(map "$3")"; dst="$(map "$4")"
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    ;;
  *)
    echo "fake-aws: unsupported command: $*" >&2
    exit 2
    ;;
esac
