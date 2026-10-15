#!/usr/bin/env bash
#
# Copyright 2025 Amazon Web Services, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Serve the built HMI bundle DETACHED from the LocalServer and from Docker.
#
# The bundle is static files, so any HTTP server will do; this uses the Python
# 3 standard library so a device needs nothing installed beyond python3 (no
# Node, no nginx, no container).
#
# The LocalServer is still the API, and a detached page has to be told where it
# is. This script STAMPS that into the served HTML's `dda-api-base` meta tag
# (API_BASE, default `5000` = the LocalServer on this host), so the kiosk URL
# needs no `?api=` query string. Without it the page would ask its own static
# server for `/local-auth/status`, get a 404, and sit on the login form.
#
# A per-URL `?api=<port|origin>` still overrides that, but only for this
# page's host, private-network, loopback and link-local IP addresses, .local
# and single-label names, and the hosts in API_ALLOW_HOSTS (stamped into the
# `dda-api-allow-hosts` meta tag). The page ignores any other host and shows a
# notice naming it, because the HMI sends its session token and the login
# form's credentials to whatever API_Base it uses.
#
# Why a server at all, rather than opening index.html from disk: the bundle
# loads its JavaScript as ES modules, and browsers refuse external module
# fetches over file:// (opaque origin), so a file:// page renders blank.
#
# Usage:
#   ./serve-hmi.sh                    # port 8081, ./dist, API on this host:5000
#   ./serve-hmi.sh 9000               # port 9000
#   PORT=9000 ./serve-hmi.sh          # same, via environment
#   ./serve-hmi.sh 9000 /path/to/dist # explicit bundle directory
#   BIND=127.0.0.1 ./serve-hmi.sh     # loopback only (default: all interfaces)
#
#   API_BASE=5000                     # default: LocalServer on this host:5000
#   API_BASE=http://192.168.8.224:5000  # LocalServer on another machine
#   API_BASE= ./serve-hmi.sh          # stamp nothing; same-origin as before
#
#   API_ALLOW_HOSTS=station-01.plant.example,.plant.example,100.64.0.0/10
#                                     # more hosts ?api= may name: exact names
#                                     # or IPs, .suffix domains, IPv4 CIDRs
#                                     # (default: none beyond the built-in set)
#
# Then open, e.g.:
#   http://<host>:8081/triple.html
#   http://<host>:8081/index.html
set -euo pipefail

PORT="${1:-${PORT:-8081}}"
DIST_DIR="${2:-${DIST_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/dist}}"
BIND="${BIND:-0.0.0.0}"
# `-` not `:-` so an explicitly empty API_BASE means "stamp nothing", while an
# unset one gets the same-host default.
API_BASE="${API_BASE-5000}"
# Unset or empty: stamp nothing, so only the built-in ?api= hosts apply.
API_ALLOW_HOSTS="${API_ALLOW_HOSTS:-}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
    echo "serve-hmi: '$PORT' is not a valid port (1-65535)." >&2
    exit 2
fi

# Reject a malformed API_BASE here rather than serving a page whose requests
# quietly go nowhere. These are the forms base.ts::normalizeApiBase accepts.
if [ -n "$API_BASE" ] \
   && ! [[ "$API_BASE" =~ ^[0-9]{1,5}$ ]] \
   && ! [[ "$API_BASE" =~ ^(https?:)?//[^/]+ ]]; then
    echo "serve-hmi: API_BASE='$API_BASE' is not a port, //host:port, or http(s)://host:port." >&2
    exit 2
fi

# Same for API_ALLOW_HOSTS: every comma-separated entry must be one of the forms
# api/hosts.ts accepts. The page would silently ignore anything else, and the
# typo would only show up later as a rejected ?api= host.
if [ -n "$API_ALLOW_HOSTS" ]; then
    IFS=',' read -r -a ALLOW_ENTRIES <<< "$API_ALLOW_HOSTS"
    for entry in "${ALLOW_ENTRIES[@]}"; do
        read -r entry <<< "$entry"   # trim surrounding whitespace
        [ -n "$entry" ] || continue
        if ! [[ "$entry" =~ ^\.?[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.?$ ]] \
           && ! [[ "$entry" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]] \
           && ! [[ "$entry" =~ ^\[[0-9A-Fa-f:.]+\]$ ]]; then
            echo "serve-hmi: API_ALLOW_HOSTS entry '$entry' is not a host name or IP address," >&2
            echo "           a .suffix domain, an [IPv6] address, or an IPv4 CIDR (a.b.c.d/n)." >&2
            exit 2
        fi
    done
fi

if [ ! -d "$DIST_DIR" ]; then
    echo "serve-hmi: no bundle at '$DIST_DIR'." >&2
    echo "           Build it first:  npm ci && npm run build" >&2
    echo "           (or pass the directory: ./serve-hmi.sh $PORT /path/to/dist)" >&2
    exit 2
fi

for entry in index.html triple.html; do
    [ -f "$DIST_DIR/$entry" ] || echo "serve-hmi: warning - '$DIST_DIR/$entry' is missing." >&2
done

if command -v python3 >/dev/null 2>&1; then
    PY=python3
elif command -v python >/dev/null 2>&1; then
    PY=python
else
    echo "serve-hmi: python3 not found; it is the only requirement." >&2
    exit 2
fi

# A bundle built with the default base ("/hmi/") requests its assets from
# /hmi/assets/..., which does NOT exist at a server root - the page would load
# and then render blank. Detect that here instead of leaving a blank screen to
# debug: serve such a bundle under a /hmi/ path prefix so those URLs resolve.
NEEDS_PREFIX=0
if [ -f "$DIST_DIR/index.html" ] && grep -q 'src="/hmi/assets/' "$DIST_DIR/index.html" 2>/dev/null; then
    NEEDS_PREFIX=1
fi

# Serve from a COPY whenever the HTML has to be altered, so the built dist/ is
# never mutated: it stays reusable, works read-only, and repeated runs with
# different API_BASE values can't stack on each other.
SERVE_ROOT="$DIST_DIR"
PREFIX=""
if [ "$NEEDS_PREFIX" = 1 ] || [ -n "$API_BASE" ] || [ -n "$API_ALLOW_HOSTS" ]; then
    SERVE_ROOT="$(mktemp -d)"
    trap 'rm -rf "$SERVE_ROOT"' EXIT
    if [ "$NEEDS_PREFIX" = 1 ]; then
        mkdir -p "$SERVE_ROOT/hmi"
        cp -r "$DIST_DIR/." "$SERVE_ROOT/hmi/"
        PREFIX="hmi/"
        echo "serve-hmi: bundle was built with base=/hmi/; serving it under /hmi/ so its"
        echo "           absolute asset URLs resolve. (Build with --base=./ to serve at the root.)"
    else
        cp -r "$DIST_DIR/." "$SERVE_ROOT/"
    fi
fi

# Stamp the API_Base, and the ?api= allow list when set, into every served HTML
# page: rewrite each meta tag's content when the tag exists, insert the tag when
# it does not. The API_Base is the documented deploy-time knob (base.ts
# precedence step 2), and it is what makes the plain kiosk URL work with no
# query string. Arguments after the root are (label, meta name, value) triples.
STAMPS=()
if [ -n "$API_BASE" ]; then
    STAMPS+=("API_Base" "dda-api-base" "$API_BASE")
fi
if [ -n "$API_ALLOW_HOSTS" ]; then
    STAMPS+=("API_ALLOW_HOSTS" "dda-api-allow-hosts" "$API_ALLOW_HOSTS")
fi
if [ "${#STAMPS[@]}" -gt 0 ]; then
    "$PY" - "$SERVE_ROOT" "${STAMPS[@]}" <<'PYSTAMP' || { echo "serve-hmi: failed to stamp API_BASE / API_ALLOW_HOSTS into the HTML." >&2; exit 2; }
import html
import pathlib
import re
import sys

root, args = pathlib.Path(sys.argv[1]), sys.argv[2:]
head = re.compile(r'<head[^>]*>', re.IGNORECASE)
pages = sorted(root.rglob("*.html"))

for i in range(0, len(args), 3):
    label, name, value = args[i:i + 3]
    tag = f'<meta name="{name}" content="{html.escape(value, quote=True)}" />'
    existing = re.compile(r'<meta\s+name="%s"[^>]*>' % re.escape(name), re.IGNORECASE)
    stamped = []
    for page in pages:
        text = page.read_text(encoding="utf-8")
        # Replacement via a function, so the value is never read as a regex template.
        if existing.search(text):
            updated = existing.sub(lambda _m: tag, text, count=1)
        elif head.search(text):
            updated = head.sub(lambda m: m.group(0) + "\n    " + tag, text, count=1)
        else:
            print(f"serve-hmi: warning - no <head> in {page.name}; {label} not stamped.",
                  file=sys.stderr)
            continue
        if updated != text:
            page.write_text(updated, encoding="utf-8")
        stamped.append(page.name)
    print("serve-hmi: %s '%s' stamped into: %s" % (label, value, ", ".join(stamped) or "(nothing)"))
PYSTAMP
fi

HOSTNAME_GUESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$HOSTNAME_GUESS" ] || HOSTNAME_GUESS="localhost"

echo "serve-hmi: serving $DIST_DIR on http://$BIND:$PORT/  (Ctrl-C to stop)"
echo
echo "  3-pane kiosk : http://$HOSTNAME_GUESS:$PORT/${PREFIX}triple.html"
echo "  single view  : http://$HOSTNAME_GUESS:$PORT/${PREFIX}index.html"
echo
if [ -n "$API_BASE" ]; then
    echo "  API requests go to '$API_BASE' (stamped into the pages; no ?api= needed)."
else
    echo "  API_BASE is empty, so the pages call their OWN origin (:$PORT), which serves"
    echo "  static files only. Append '?api=5000' to reach the LocalServer, or restart"
    echo "  with API_BASE=5000 to stamp it in."
fi
echo "  A per-URL '?api=<port|origin>' is used only for this host, private-network,"
echo "  loopback and link-local IP addresses, and .local or single-label names."
if [ -n "$API_ALLOW_HOSTS" ]; then
    echo "  Also allowed (API_ALLOW_HOSTS): $API_ALLOW_HOSTS"
else
    echo "  Allow more hosts with API_ALLOW_HOSTS=<host,.suffix,a.b.c.d/n>."
fi
echo "  Any other ?api= host is ignored, and the page shows a notice naming it."
echo

# Deliberately NOT `exec`: the serve root can be a temp copy, and `exec` would
# replace this shell so the EXIT trap that removes it never runs, leaking a
# bundle copy under /tmp on every restart.
"$PY" -m http.server "$PORT" --bind "$BIND" --directory "$SERVE_ROOT"
