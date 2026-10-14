#!/usr/bin/env bash
# dda-plugin-git-sync runner (custom-node-source-lifecycle, design §6).
#
# Executes one Sync_Operation inside the `dda-plugin-git-sync` CodeBuild
# project: SYNC_KIND=verify | push | pull. It ALWAYS writes a structured
# result to $RESULT_FILE (default /tmp/result.json) that the post_build
# phase uploads to s3://$ARTIFACTS_BUCKET/$RESULT_KEY; the GitSyncHandler
# Lambda reads it to settle the operation. Controlled outcomes (diverged,
# not_found, authentication, ...) exit 0 so the build reports SUCCEEDED and
# the category travels in result.json; only crashes produce FAILED.
#
# Environment (StartBuild overrides from git_sync.py):
#   SYNC_KIND, OPERATION_ID, REPO_URL, GIT_PROVIDER, GIT_USERNAME,
#   GIT_TOKEN (type SECRETS_MANAGER - resolved by CodeBuild, masked in logs),
#   BRANCH, DEFAULT_BRANCH, REPO_PATH,
#   push: SOURCE_PREFIX, LAST_SYNC_COMMIT, FORCE (0|1), COMMIT_MESSAGE,
#         MANIFEST_JSON
#   pull: REF, STAGING_PREFIX
#   ARTIFACTS_BUCKET, RESULT_KEY
#
# Test hooks (offline shell tests): RESULT_FILE, WORK_DIR, and an `aws`
# stub on PATH implementing `s3 sync` / `s3 cp` against local directories.
#
# The token never appears in a URL or on a command line: git authenticates
# through GIT_ASKPASS, which echoes $GIT_TOKEN from the environment.
set -eu -o pipefail

RESULT_FILE="${RESULT_FILE:-/tmp/result.json}"
WORK_DIR="${WORK_DIR:-/tmp/dda-git-sync}"
SYNC_KIND="${SYNC_KIND:-}"
FORCE="${FORCE:-0}"
MAX_TREE_FILES="${MAX_TREE_FILES:-2000}"
MAX_TREE_BYTES="${MAX_TREE_BYTES:-52428800}"   # 50 MiB
MANIFEST_NAME="dda-plugin.json"

RESULT_WRITTEN=0
LAST_STDERR=""

# ------------------------------------------------------------- result.json

json_escape() {
  # Escape a string for inclusion in a JSON document (no external deps).
  python3 - "$1" <<'PY'
import json, sys
sys.stdout.write(json.dumps(sys.argv[1]))
PY
}

json_string_array() {
  # Newline-separated stdin -> JSON array of strings.
  python3 -c 'import json, sys; print(json.dumps([l for l in sys.stdin.read().split("\n") if l]))'
}

write_result() {
  # write_result ok|fail [key=value ...]  (values are raw JSON fragments)
  local ok="$1"; shift
  local fields=""
  local kv key value
  for kv in "$@"; do
    key="${kv%%=*}"; value="${kv#*=}"
    fields="${fields}, \"${key}\": ${value}"
  done
  local okjson="false"
  [ "$ok" = "ok" ] && okjson="true"
  printf '{"ok": %s, "kind": %s%s}\n' "$okjson" "$(json_escape "$SYNC_KIND")" "$fields" > "$RESULT_FILE"
  RESULT_WRITTEN=1
}

fail_result() {
  # fail_result <category> <message> [extra key=value ...]
  local category="$1"; local message="$2"; shift 2
  write_result fail "category=$(json_escape "$category")" "message=$(json_escape "$message")" "$@"
  echo "git-sync: $category: $message" >&2
  exit 0
}

on_exit() {
  local code=$?
  if [ "$RESULT_WRITTEN" -eq 0 ]; then
    local msg="runner exited with status $code"
    [ -n "$LAST_STDERR" ] && msg="$msg: $(printf '%s' "$LAST_STDERR" | tail -c 2000)"
    write_result fail "category=\"internal\"" "message=$(json_escape "$msg")" || true
  fi
}
trap on_exit EXIT

# ------------------------------------------------------------- git helpers

classify() {
  # classify <stderr-text> -> authentication | not_found | unreachable | internal
  local text
  text="$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')"
  case "$text" in
    *"authentication failed"*|*"could not read username"*|*"could not read password"*|*" 401"*|*"http 401"*|*" 403"*|*"http 403"*|*"invalid username or password"*|*"permission denied"*)
      echo authentication ;;
    *"repository not found"*|*"not found"*|*" 404"*|*"http 404"*|*"couldn't find remote ref"*|*"could not find remote branch"*|*"pathspec"*|*"does not appear to be a git repository"*|*"remote branch"*"not found"*)
      echo not_found ;;
    *"could not resolve host"*|*"connection timed out"*|*"unable to access"*|*"failed to connect"*|*"connection refused"*|*"network is unreachable"*|*"operation timed out"*)
      echo unreachable ;;
    *) echo internal ;;
  esac
}

run_git() {
  # run_git <args...>: run git, capturing stderr into LAST_STDERR; returns git's status.
  local err_file
  err_file="$(mktemp)"
  set +e
  git "$@" 2>"$err_file"
  local status=$?
  set -e
  LAST_STDERR="$(cat "$err_file")"
  rm -f "$err_file"
  if [ -n "$LAST_STDERR" ]; then
    printf '%s\n' "$LAST_STDERR" >&2
  fi
  return $status
}

setup_auth() {
  # git authenticates through GIT_ASKPASS; the token is never on a command
  # line or in the remote URL (design §6, Requirement 2.4).
  mkdir -p "$WORK_DIR"
  cat > "$WORK_DIR/askpass.sh" <<'ASK'
#!/usr/bin/env bash
case "$1" in
  *[Uu]sername*) printf '%s\n' "${GIT_USERNAME:-x-access-token}" ;;
  *) printf '%s\n' "${GIT_TOKEN:-}" ;;
esac
ASK
  chmod +x "$WORK_DIR/askpass.sh"
  export GIT_ASKPASS="$WORK_DIR/askpass.sh"
  export GIT_TERMINAL_PROMPT=0
  # An isolated HOME keeps host credential helpers out of the picture and
  # works on every git version (GIT_CONFIG_COUNT needs git >= 2.31).
  export HOME="$WORK_DIR/home"
  mkdir -p "$HOME"
  git config --global credential.helper ""
  git config --global credential.username "${GIT_USERNAME:-x-access-token}"
  git config --global advice.detachedHead false
  git config --global init.defaultBranch main
  export GIT_AUTHOR_NAME="DDA Portal" GIT_AUTHOR_EMAIL="dda-portal@noreply.local"
  export GIT_COMMITTER_NAME="DDA Portal" GIT_COMMITTER_EMAIL="dda-portal@noreply.local"
}

require() {
  local name
  for name in "$@"; do
    if [ -z "${!name:-}" ]; then
      fail_result internal "missing required variable $name"
    fi
  done
}

validate_repo_path() {
  # REPO_PATH must be relative, non-empty, and free of `..` segments.
  case "/$REPO_PATH/" in
    *"/../"*|"//"*|*"//"*) fail_result internal "invalid REPO_PATH '$REPO_PATH'" ;;
  esac
  [ "$REPO_PATH" != "." ] || fail_result internal "invalid REPO_PATH '.'"
}

tree_stats() {
  # tree_stats <dir> -> "files bytes" of regular files under dir
  local dir="$1"
  local files bytes
  files="$(find "$dir" -type f | wc -l | tr -d ' ')"
  bytes="$(find "$dir" -type f -printf '%s\n' 2>/dev/null | awk '{s+=$1} END {print s+0}')"
  echo "$files $bytes"
}

# -------------------------------------------------------------------- verify

do_verify() {
  require REPO_URL
  local out
  set +e
  out="$(git ls-remote --symref "$REPO_URL" HEAD 2>&1)"
  local status=$?
  set -e
  if [ $status -ne 0 ]; then
    LAST_STDERR="$out"
    fail_result "$(classify "$out")" "$(printf '%s' "$out" | tail -c 1000)"
  fi
  local default_branch
  default_branch="$(printf '%s\n' "$out" | sed -n 's#^ref: refs/heads/\([^[:space:]]*\)[[:space:]]*HEAD$#\1#p' | head -n1)"
  write_result ok "default_branch=$(json_escape "${default_branch:-}")"
}

# ---------------------------------------------------------------------- push

clone_for_push() {
  # Clone BRANCH; create it from DEFAULT_BRANCH when missing; init when the
  # repository is empty (Requirement 3.5). Sets BRANCH_CREATED=1 when the
  # branch did not exist remotely.
  BRANCH_CREATED=0
  rm -rf "$WORK_DIR/repo"
  if run_git clone --quiet --branch "$BRANCH" --single-branch "$REPO_URL" "$WORK_DIR/repo"; then
    return 0
  fi
  local first_err="$LAST_STDERR"
  local category
  category="$(classify "$first_err")"
  if [ "$category" = "authentication" ] || [ "$category" = "unreachable" ]; then
    fail_result "$category" "$(printf '%s' "$first_err" | tail -c 1000)"
  fi
  # The branch may simply not exist yet: clone the default branch instead.
  if [ -n "${DEFAULT_BRANCH:-}" ] && [ "$DEFAULT_BRANCH" != "$BRANCH" ] \
      && run_git clone --quiet --branch "$DEFAULT_BRANCH" --single-branch "$REPO_URL" "$WORK_DIR/repo"; then
    run_git -C "$WORK_DIR/repo" checkout --quiet -b "$BRANCH"
    BRANCH_CREATED=1
    return 0
  fi
  # Empty repository (no branches at all): clone succeeds without --branch.
  rm -rf "$WORK_DIR/repo"
  if run_git clone --quiet "$REPO_URL" "$WORK_DIR/repo"; then
    if [ -z "$(git -C "$WORK_DIR/repo" branch --list 2>/dev/null)" ]; then
      run_git -C "$WORK_DIR/repo" checkout --quiet --orphan "$BRANCH" || true
      run_git -C "$WORK_DIR/repo" symbolic-ref HEAD "refs/heads/$BRANCH"
      BRANCH_CREATED=1
      return 0
    fi
    # Repository has branches but neither BRANCH nor DEFAULT_BRANCH exists.
    run_git -C "$WORK_DIR/repo" checkout --quiet -b "$BRANCH"
    BRANCH_CREATED=1
    return 0
  fi
  fail_result "$(classify "$LAST_STDERR")" "$(printf '%s' "$LAST_STDERR" | tail -c 1000)"
}

divergence_guard() {
  # Requirement 3.7: refuse when REPO_PATH changed in the repository since
  # the last recorded sync commit, unless FORCE=1. An unreachable last-sync
  # commit counts as diverged (the history we synchronized against is gone).
  [ "$BRANCH_CREATED" -eq 0 ] || return 0
  [ -n "${LAST_SYNC_COMMIT:-}" ] || return 0
  [ "$FORCE" != "1" ] || return 0
  local repo="$WORK_DIR/repo"
  if ! git -C "$repo" cat-file -e "${LAST_SYNC_COMMIT}^{commit}" 2>/dev/null; then
    # A shallow single-branch clone may not carry the commit; deepen once.
    run_git -C "$repo" fetch --quiet --unshallow origin "$BRANCH" 2>/dev/null || true
  fi
  if ! git -C "$repo" cat-file -e "${LAST_SYNC_COMMIT}^{commit}" 2>/dev/null; then
    fail_result diverged \
      "the last synchronized commit ${LAST_SYNC_COMMIT} is not reachable from ${BRANCH}; pull first or push with overwrite" \
      "changed_files=[\"<unknown>\"]" "last_sync_commit=$(json_escape "$LAST_SYNC_COMMIT")"
  fi
  if ! git -C "$repo" diff --quiet "$LAST_SYNC_COMMIT" HEAD -- "$REPO_PATH"; then
    local changed
    changed="$(git -C "$repo" diff --name-only "$LAST_SYNC_COMMIT" HEAD -- "$REPO_PATH" | json_string_array)"
    fail_result diverged \
      "the repository changed under ${REPO_PATH} since the last sync; pull first or push with overwrite" \
      "changed_files=${changed}" "last_sync_commit=$(json_escape "$LAST_SYNC_COMMIT")"
  fi
}

replace_repo_path() {
  # Wholesale replacement of REPO_PATH with the Source_Tree + Sync_Manifest
  # (Requirements 3.3, 3.4): nothing outside REPO_PATH is touched.
  local repo="$WORK_DIR/repo"
  run_git -C "$repo" rm -r -q --cached --ignore-unmatch -- "$REPO_PATH" || true
  rm -rf "$repo/$REPO_PATH"
  mkdir -p "$repo/$REPO_PATH"
  if [ -n "$(ls -A "$WORK_DIR/src" 2>/dev/null)" ]; then
    cp -a "$WORK_DIR/src/." "$repo/$REPO_PATH/"
  fi
  if [ -n "${MANIFEST_JSON:-}" ]; then
    printf '%s\n' "$MANIFEST_JSON" > "$repo/$REPO_PATH/$MANIFEST_NAME"
  fi
  run_git -C "$repo" add -A -- "$REPO_PATH"
}

do_push() {
  require REPO_URL BRANCH REPO_PATH SOURCE_PREFIX ARTIFACTS_BUCKET
  validate_repo_path
  rm -rf "$WORK_DIR/src"; mkdir -p "$WORK_DIR/src"
  aws s3 sync "s3://$ARTIFACTS_BUCKET/$SOURCE_PREFIX" "$WORK_DIR/src" --exact-timestamps --quiet \
    || fail_result internal "could not download the plugin source tree from s3://$ARTIFACTS_BUCKET/$SOURCE_PREFIX"

  clone_for_push
  divergence_guard
  replace_repo_path

  local repo="$WORK_DIR/repo"
  if git -C "$repo" rev-parse --verify --quiet HEAD >/dev/null && git -C "$repo" diff --cached --quiet; then
    # Requirement 3.6: identical contents -> no commit.
    write_result ok "commit=$(json_escape "$(git -C "$repo" rev-parse HEAD)")" \
      "no_changes=true" "files=$(git -C "$repo" ls-files -- "$REPO_PATH" | wc -l | tr -d ' ')" \
      "branch=$(json_escape "$BRANCH")" "path=$(json_escape "$REPO_PATH")"
    return 0
  fi

  run_git -C "$repo" commit --quiet -m "${COMMIT_MESSAGE:-DDA Portal: sync plugin source}" \
    || fail_result internal "commit failed: $(printf '%s' "$LAST_STDERR" | tail -c 1000)"

  if ! run_git -C "$repo" push --quiet origin "HEAD:refs/heads/$BRANCH"; then
    local first_err="$LAST_STDERR"
    local category
    category="$(classify "$first_err")"
    if [ "$category" = "authentication" ] || [ "$category" = "unreachable" ]; then
      fail_result "$category" "$(printf '%s' "$first_err" | tail -c 1000)"
    fi
    # Requirement 3.9: the branch advanced during the operation - fetch,
    # rebase our single commit, retry once.
    if run_git -C "$repo" fetch --quiet origin "$BRANCH" \
        && run_git -C "$repo" rebase --quiet "origin/$BRANCH" \
        && run_git -C "$repo" push --quiet origin "HEAD:refs/heads/$BRANCH"; then
      :
    else
      git -C "$repo" rebase --abort >/dev/null 2>&1 || true
      fail_result push_rejected \
        "the remote rejected the push after one retry: $(printf '%s' "$LAST_STDERR" | tail -c 1000)"
    fi
  fi

  write_result ok "commit=$(json_escape "$(git -C "$repo" rev-parse HEAD)")" \
    "no_changes=false" \
    "files=$(git -C "$repo" ls-files -- "$REPO_PATH" | wc -l | tr -d ' ')" \
    "branch=$(json_escape "$BRANCH")" "path=$(json_escape "$REPO_PATH")" \
    "branch_created=$([ "$BRANCH_CREATED" -eq 1 ] && echo true || echo false)"
}

# ---------------------------------------------------------------------- pull

do_pull() {
  require REPO_URL REPO_PATH STAGING_PREFIX ARTIFACTS_BUCKET
  validate_repo_path
  local ref="${REF:-${BRANCH:-}}"
  [ -n "$ref" ] || fail_result internal "no ref to pull (REF and BRANCH are empty)"
  local repo="$WORK_DIR/repo"
  rm -rf "$repo"; mkdir -p "$repo"
  run_git -C "$repo" init --quiet
  run_git -C "$repo" remote add origin "$REPO_URL"
  # Shallow fetch of a branch, tag, or reachable commit; full fetch as the
  # fallback for servers that refuse shallow SHA fetches.
  if ! run_git -C "$repo" fetch --quiet --depth 1 origin "$ref"; then
    local first_err="$LAST_STDERR"
    local category
    category="$(classify "$first_err")"
    if [ "$category" = "authentication" ] || [ "$category" = "unreachable" ]; then
      fail_result "$category" "$(printf '%s' "$first_err" | tail -c 1000)"
    fi
    if ! run_git -C "$repo" fetch --quiet origin "$ref"; then
      fail_result not_found "ref '$ref' does not exist in the repository: $(printf '%s' "$LAST_STDERR" | tail -c 1000)" \
        "ref=$(json_escape "$ref")"
    fi
  fi
  run_git -C "$repo" checkout --quiet FETCH_HEAD
  local commit
  commit="$(git -C "$repo" rev-parse FETCH_HEAD)"

  local tree="$repo/$REPO_PATH"
  if [ ! -d "$tree" ] || [ -z "$(ls -A "$tree")" ]; then
    fail_result not_found "path '$REPO_PATH' is absent or empty at $ref" \
      "ref=$(json_escape "$ref")" "commit=$(json_escape "$commit")"
  fi
  # Requirement 4.3: exclude the Sync_Manifest, .git directories, symlinks.
  find "$tree" -type l -delete
  find "$tree" -name .git -prune -exec rm -rf {} + 2>/dev/null || true
  rm -f "$tree/$MANIFEST_NAME"
  if [ -z "$(ls -A "$tree")" ]; then
    fail_result not_found "path '$REPO_PATH' holds no source files at $ref" \
      "ref=$(json_escape "$ref")" "commit=$(json_escape "$commit")"
  fi
  read -r files bytes <<<"$(tree_stats "$tree")"
  if [ "$files" -gt "$MAX_TREE_FILES" ]; then
    fail_result invalid_source "the tree has $files files; at most $MAX_TREE_FILES are allowed" \
      "limit=\"files\"" "tree_files=$files" "tree_bytes=$bytes"
  fi
  if [ "$bytes" -gt "$MAX_TREE_BYTES" ]; then
    fail_result invalid_source "the tree is $bytes bytes; at most $MAX_TREE_BYTES are allowed" \
      "limit=\"bytes\"" "tree_files=$files" "tree_bytes=$bytes"
  fi

  aws s3 sync "$tree/" "s3://$ARTIFACTS_BUCKET/$STAGING_PREFIX" --delete --quiet \
    || fail_result internal "could not upload the pulled tree to s3://$ARTIFACTS_BUCKET/$STAGING_PREFIX"

  write_result ok "commit=$(json_escape "$commit")" "ref=$(json_escape "$ref")" \
    "tree_files=$files" "tree_bytes=$bytes" "path=$(json_escape "$REPO_PATH")"
}

# ------------------------------------------------------------------ dispatch

main() {
  mkdir -p "$WORK_DIR"
  setup_auth
  case "$SYNC_KIND" in
    verify) do_verify ;;
    push)   do_push ;;
    pull)   do_pull ;;
    *)      fail_result internal "unknown SYNC_KIND '$SYNC_KIND'" ;;
  esac
}

# Run only when executed (not when sourced by the shell tests, which call
# classify / write_result directly).
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  main "$@"
else
  trap - EXIT
fi
