#!/usr/bin/env bash
# dda-plugin-fetch: clone a plugin repository and sync its tree to S3.
#
# Runs inside the dda-plugin-fetch CodeBuild project (node-designer-stack.ts
# inlines this file into the project's buildspec at synth time, so the
# project stays NO_SOURCE and needs no extra read grant). Invoked by
# plugin_importer.start_fetch through StartBuild environment overrides:
#
#   REPO_URL      clone target (required)
#   DEST_PREFIX   s3 prefix that receives the tree, no trailing slash (required)
#   REVISION      optional tag / commit / branch to check out after cloning
#   REPO_BRANCH   optional branch to clone (Git_Connection imports;
#                 private-repo-plugin-import 1.6)
#   REPO_SUBDIR   optional repository subdirectory to sync instead of the
#                 whole tree (1.5)
#   SHALLOW       non-empty = depth-1 clone (1.8)
#   GIT_TOKEN     SECRETS_MANAGER-resolved credential (Authenticated_Fetch,
#                 2.1); absent = anonymous public clone, exactly as before
#   GIT_USERNAME  basic-auth username git sends with the token
#   RESULT_KEY    optional s3 key for result.json (written on every exit
#                 once set; the Lambda classifies failures from it, 3.1)
#
# Credential handling (2.2): the token reaches git only through GIT_ASKPASS
# - a helper that echoes the environment variable when git prompts - so it
# is never on a command line, in a remote URL, or in a file that outlives
# the build. GIT_TERMINAL_PROMPT=0 (set for every fetch) turns a rejected
# or missing credential into an immediate failure instead of a hung build.
#
# Anonymous, option-free invocations perform the same operations as the
# original five-line buildspec: `git clone "$REPO_URL" /tmp/repo`, optional
# `git checkout "$REVISION"`, `rm -rf .git`, `aws s3 sync`.
set -uo pipefail

: "${ARTIFACTS_BUCKET:?ARTIFACTS_BUCKET is required}"
: "${REPO_URL:?REPO_URL is required}"
: "${DEST_PREFIX:?DEST_PREFIX is required}"
REVISION="${REVISION:-}"
REPO_BRANCH="${REPO_BRANCH:-}"
REPO_SUBDIR="${REPO_SUBDIR:-}"
SHALLOW="${SHALLOW:-}"
RESULT_KEY="${RESULT_KEY:-}"
GIT_USERNAME="${GIT_USERNAME:-x-access-token}"

WORK_DIR="${FETCH_WORK_DIR:-/tmp/dda-fetch}"
REPO_DIR="${FETCH_REPO_DIR:-/tmp/repo}"
rm -rf "$WORK_DIR" "$REPO_DIR"
mkdir -p "$WORK_DIR"
ERR_LOG="$WORK_DIR/stderr.log"
: > "$ERR_LOG"

STATUS=failed
COMMIT=""
BRANCH_RESOLVED=""
MARKER=""

# ------------------------------------------------------------------ result
write_result() {
  # Always attempt when a RESULT_KEY was given; failures here never mask
  # the fetch outcome (the CodeBuild build status still carries it).
  [ -n "$RESULT_KEY" ] || return 0
  tail -c 8192 "$ERR_LOG" > "$WORK_DIR/tail.txt" 2>/dev/null || : > "$WORK_DIR/tail.txt"
  python3 - "$STATUS" "$COMMIT" "$BRANCH_RESOLVED" "$MARKER" "$WORK_DIR/tail.txt" \
      > "$WORK_DIR/result.json" <<'PY'
import json, sys
status, commit, branch, marker, tail_file = sys.argv[1:6]
with open(tail_file, errors="replace") as fh:
    tail = fh.read()
json.dump({
    "status": status,
    "commit": commit or None,
    "branch": branch or None,
    "failure_marker": marker or None,
    "stderr_tail": tail,
}, sys.stdout)
PY
  if [ -n "${RESULT_FILE:-}" ]; then
    cp "$WORK_DIR/result.json" "$RESULT_FILE"
  fi
  aws s3 cp "$WORK_DIR/result.json" "s3://$ARTIFACTS_BUCKET/$RESULT_KEY" \
      --only-show-errors >/dev/null 2>>"$ERR_LOG" \
    || echo "dda-plugin-fetch: WARN result upload failed" >&2
}

fail() {
  MARKER="$1"
  echo "$1: $2" >> "$ERR_LOG"
  cat "$ERR_LOG" >&2
  echo "dda-plugin-fetch: FAILED ($1): $2" >&2
  write_result
  exit 1
}

# ------------------------------------------------------------- credentials
# Never wait on a terminal prompt (CodeBuild has none): a repository that
# demands credentials we do not hold fails immediately, whichever kind of
# fetch this is.
export GIT_TERMINAL_PROMPT=0

if [ -n "${GIT_TOKEN:-}" ]; then
  # Isolated HOME so nothing leaks into (or reads from) the build image's
  # git configuration; an empty credential.helper resets any helper the
  # image might configure, so the askpass helper is git's ONLY credential
  # source - it answers both the username and the password prompt from
  # the environment, and the token is never written anywhere.
  export HOME="$WORK_DIR/home"
  mkdir -p "$HOME"
  printf '[credential]\n\thelper =\n' > "$HOME/.gitconfig"
  ASKPASS="$WORK_DIR/askpass.sh"
  printf '%s\n' '#!/bin/sh' \
    'case "$1" in' \
    '  *sername*) printf "%s\n" "$GIT_USERNAME" ;;' \
    '  *) printf "%s\n" "$GIT_TOKEN" ;;' \
    'esac' > "$ASKPASS"
  chmod 700 "$ASKPASS"
  export GIT_ASKPASS="$ASKPASS"
  echo "dda-plugin-fetch: authenticated fetch (token via askpass)"
fi

# -------------------------------------------------------------------- clone
CLONE_ARGS=()
if [ -n "$SHALLOW" ]; then
  CLONE_ARGS+=(--depth 1)
fi
if [ -n "$REPO_BRANCH" ]; then
  CLONE_ARGS+=(--branch "$REPO_BRANCH")
fi
echo "dda-plugin-fetch: cloning${REPO_BRANCH:+ branch $REPO_BRANCH}${SHALLOW:+ (shallow)}"
if ! git clone --quiet "${CLONE_ARGS[@]}" "$REPO_URL" "$REPO_DIR" 2>>"$ERR_LOG"; then
  if [ -n "$REPO_BRANCH" ] && grep -qi "remote branch .* not found\|could not find remote branch" "$ERR_LOG"; then
    fail BRANCH_NOT_FOUND "branch '$REPO_BRANCH' not found in the repository"
  fi
  fail CLONE_FAILED "git clone failed"
fi

# ----------------------------------------------------------------- revision
if [ -n "$REVISION" ]; then
  if [ -n "$SHALLOW" ]; then
    # Depth-1 fetch of the named revision (tags and branches always; bare
    # SHAs when the host allows reachable-SHA fetches), else deepen fully.
    if git -C "$REPO_DIR" fetch --quiet --depth 1 origin -- "$REVISION" 2>>"$ERR_LOG"; then
      git -C "$REPO_DIR" checkout --quiet FETCH_HEAD 2>>"$ERR_LOG" \
        || fail REVISION_NOT_FOUND "revision '$REVISION' could not be checked out"
    else
      git -C "$REPO_DIR" fetch --quiet --unshallow origin 2>>"$ERR_LOG" || :
      git -C "$REPO_DIR" fetch --quiet --tags origin 2>>"$ERR_LOG" || :
      git -C "$REPO_DIR" checkout --quiet "$REVISION" 2>>"$ERR_LOG" \
        || fail REVISION_NOT_FOUND "revision '$REVISION' not found"
    fi
  else
    git -C "$REPO_DIR" checkout --quiet "$REVISION" 2>>"$ERR_LOG" \
      || fail REVISION_NOT_FOUND "revision '$REVISION' not found"
  fi
fi

COMMIT="$(git -C "$REPO_DIR" rev-parse HEAD 2>>"$ERR_LOG" || true)"
BRANCH_RESOLVED="$(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>>"$ERR_LOG" || true)"
if [ "$BRANCH_RESOLVED" = "HEAD" ]; then
  BRANCH_RESOLVED="$REPO_BRANCH"
fi

# ------------------------------------------------------------- subdirectory
SRC_DIR="$REPO_DIR"
if [ -n "$REPO_SUBDIR" ]; then
  # Same rule as the Lambda's normalize_source_path: relative, not '.',
  # and no '..' SEGMENT (a directory literally named 'a..b' is fine).
  case "/${REPO_SUBDIR%/}/" in
    //*|*/../*|/./) fail INVALID_SUBDIR "subdirectory '$REPO_SUBDIR' is not a relative path" ;;
  esac
  SRC_DIR="$REPO_DIR/${REPO_SUBDIR%/}"
  if [ ! -d "$SRC_DIR" ]; then
    fail PATH_NOT_FOUND "subdirectory '$REPO_SUBDIR' not found in the repository"
  fi
fi

# --------------------------------------------------------------------- sync
rm -rf "$REPO_DIR/.git"
if ! aws s3 sync "$SRC_DIR/" "s3://$ARTIFACTS_BUCKET/$DEST_PREFIX/" 2>>"$ERR_LOG"; then
  fail SYNC_FAILED "aws s3 sync to $DEST_PREFIX failed"
fi

STATUS=succeeded
echo "dda-plugin-fetch: synced ${REPO_SUBDIR:-/} at ${COMMIT:0:12} to s3://$ARTIFACTS_BUCKET/$DEST_PREFIX/"
if [ -s "$ERR_LOG" ]; then
  cat "$ERR_LOG" >&2
fi
write_result
exit 0
