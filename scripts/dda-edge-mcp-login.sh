#!/usr/bin/env bash
# dda-edge-mcp-login.sh — sign in to a LocalServer with a synced local
# account and print the Local_Session_Token for an MCP client
# (mcp-servers design Decision point 3; Requirements 9.3, 9.6, 2.7).
#
# Usage:
#   eval "$(scripts/dda-edge-mcp-login.sh https://<device>:5443)"
#
# The script prompts for the username, reads the password with `read -s`
# (never from an argument or the environment), POSTs both to
# <base-url>/local-auth/login, and prints exactly one line on stdout:
#
#   export DDA_EDGE_TOKEN=<token>
#
# Everything else goes to stderr. Nothing is written to a file: the login
# response stays in a shell variable. The token is printed only when it
# consists of RFC 6750 b64token characters (a Local_Session_Token is
# base64url "." base64url), so the line holds nothing the shell would
# interpret. Under `eval "$(...)"` the token never appears on a typed
# command line, so the shell history does not record it.
#
# TLS: the certificate is verified. A station whose certificate is
# self-signed is reached with DDA_EDGE_CA_CERT=<the station's certificate
# file>; DDA_EDGE_INSECURE=1 skips verification (the password is then sent
# to whichever host answers at that address).
set -euo pipefail

say() { printf '%s\n' "$*" >&2; }

if [ "$#" -ne 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  say "usage: eval \"\$($0 https://<device>:5443)\""
  exit 2
fi
base_url="${1%/}"
case "$base_url" in
  https://*|http://localhost:*|http://127.0.0.1:*) ;;
  *)
    say "error: the base URL must be https://<device>:<port> (or http://localhost:<port> through a tunnel)"
    exit 2
    ;;
esac
for tool in curl python3; do
  command -v "$tool" >/dev/null 2>&1 || { say "error: $tool is required"; exit 1; }
done

tls_args=()
if [ -n "${DDA_EDGE_CA_CERT:-}" ]; then
  tls_args=(--cacert "$DDA_EDGE_CA_CERT")
elif [ "${DDA_EDGE_INSECURE:-}" = "1" ]; then
  say "warning: DDA_EDGE_INSECURE=1: the server certificate is not verified"
  tls_args=(--insecure)
fi

printf 'Username: ' >&2
IFS= read -r username
printf 'Password: ' >&2
IFS= read -r -s password
printf '\n' >&2
if [ -z "$username" ] || [ -z "$password" ]; then
  say "error: username and password are required"
  exit 1
fi

# The JSON body is built from stdin (printf is a shell builtin, so neither
# value appears in any process's argument list) and sent with --data-binary @-.
# The response body and, on its own last line, the HTTP status are captured
# in memory; nothing goes to disk.
set +e
response=$(printf '%s\0%s' "$username" "$password" \
  | python3 -c 'import json, sys
username, password = sys.stdin.buffer.read().decode("utf-8").split("\0", 1)
sys.stdout.write(json.dumps({"username": username, "password": password}))' \
  | curl --silent --show-error ${tls_args[@]+"${tls_args[@]}"} \
      --request POST "$base_url/local-auth/login" \
      --header 'Content-Type: application/json' \
      --data-binary @- \
      --write-out '\n%{http_code}')
curl_rc=$?
set -e
unset password

if [ "$curl_rc" -ne 0 ]; then
  say "error: could not reach $base_url (curl exit $curl_rc)"
  exit 1
fi
status=${response##*$'\n'}
body=${response%$'\n'*}
unset response
if [ "$status" != "200" ]; then
  detail=$(printf '%s' "$body" | python3 -c 'import json, sys
try:
    body = json.load(sys.stdin)
    print(body.get("detail") if isinstance(body, dict) else "")
except Exception:
    print("")')
  say "error: login failed (HTTP $status)${detail:+: $detail}"
  exit 1
fi

# Allowlist: RFC 6750 b64token, the character set of a Local_Session_Token.
# Anything else (whitespace, quotes, ; | & > < ( ) { } $ ` and so on) is
# refused with nothing on stdout.
token=$(printf '%s' "$body" | python3 -c 'import json, re, sys
try:
    body = json.load(sys.stdin)
except Exception:
    sys.exit(1)
token = body.get("token") if isinstance(body, dict) else None
if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
    sys.exit(1)
print(token)') || { say "error: the login response carried no usable token"; exit 1; }
unset body

printf 'export DDA_EDGE_TOKEN=%q\n' "$token"
say "Signed in as $username; DDA_EDGE_TOKEN is set for this shell."
say "Reminder: use this script only under eval \"\$(...)\". Pasting the export line by hand records the token in ~/.bash_history; prefix the command with a space under HISTCONTROL=ignorespace, or remove the entry with history -d."
