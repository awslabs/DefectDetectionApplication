#!/bin/bash
# Build the JP6 and JP5 LocalServer components SEQUENTIALLY via GDK.
#
# Sequential is REQUIRED (see .kiro/steering/builds.md): the two targets share
# greengrass-build/, custom-build/, and the docker image tags
# (edgemlsdk / flask-app / react-webapp), so concurrent runs clobber each other
# and corrupt model versioning. This wrapper builds one target fully before
# starting the next, swapping the component name in gdk-config.json per target
# and restoring the original config at the end. Build only (no publish).
#
# Usage:
#   ./run_jp_builds.sh                # build JP6 then JP5 (default)
#   TARGETS="6" ./run_jp_builds.sh    # build only JP6
#   TARGETS="5 6" ./run_jp_builds.sh  # build JP5 then JP6
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

COMPONENT_PREFIX="aws.edgeml.dda.LocalServer.arm64JP"
TARGETS="${TARGETS:-6 5}"

BAK=".gdk-config.json.prebuild.bak"
[ -f gdk-config.json ] && cp gdk-config.json "$BAK"

write_config () {
  local comp="$1"
  cat > gdk-config.json <<EOF
{
  "component": {
    "$comp": {
      "author": "Amazon",
      "version": "NEXT_PATCH",
      "build": {
        "build_system": "custom",
        "custom_build_command": [
          "bash",
          "build-custom.sh",
          "$comp",
          "NEXT_PATCH"
        ]
      },
      "publish": {
        "bucket": "dda-component",
        "region": "us-east-1"
      }
    }
  },
  "gdk_version": "1.0.0"
}
EOF
}

build_target () {
  local jp="$1"
  local comp="${COMPONENT_PREFIX}${jp}"
  local log=".gdk_build_jp${jp}.log"
  write_config "$comp"
  {
    echo "=================================================================="
    echo "=== START $comp  $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
    echo "=================================================================="
  } > "$log"
  gdk component build >> "$log" 2>&1
  local rc=$?
  {
    echo "=================================================================="
    echo "=== END   $comp  exit=$rc  $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="
    echo "=================================================================="
  } >> "$log"
  echo "$comp exit=$rc (log: $log)"
  return $rc
}

OVERALL=0
for jp in $TARGETS; do
  echo "### JP${jp} build starting ###"
  build_target "$jp" || OVERALL=1
done

# Restore the original gdk-config.json.
if [ -f "$BAK" ]; then
  cp "$BAK" gdk-config.json
  rm -f "$BAK"
fi

echo "### DONE (overall exit=$OVERALL) ###"
exit $OVERALL
