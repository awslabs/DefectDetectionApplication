#!/bin/bash
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
# ---------------------------------------------------------------------------
# patch_docker_host_prereqs.sh
#
# One-shot, idempotent patch for an ALREADY-PROVISIONED edge device. Applies the
# host-level prerequisites the DDA LocalServer and model components need, without
# re-running the full setup_station.sh:
#
#   1. iptables kernel modules (iptable_raw, etc.) — newer Docker (JetPack 6 /
#      Ubuntu 22.04) sets up bridge networks with a DROP rule in the iptables
#      `raw` table. Without iptable_raw loaded, container startup fails with:
#        "iptables ... can't initialize iptables table `raw': Table does not
#         exist (do you need to insmod?)"
#
#   2. Docker / iptables-raw compatibility — on stock JetPack 6 kernels the raw
#      table is unavailable; pin Docker to 27.5.1 (NVIDIA-recommended) so bridge
#      networking does not require it.
#
#   3. NVIDIA Container Runtime registration (Jetson/aarch64) — `runtime: nvidia`
#      in docker-compose requires the nvidia runtime registered in
#      /etc/docker/daemon.json. A stock docker-ce install on JP5/JP6 installs
#      nvidia-container-runtime but does NOT wire it into the daemon, so
#      containers fail with "unknown or invalid runtime name: nvidia".
#
#   4. Host protobuf for the model-conversion Startup script — the model
#      component runs `python3 /aws_dda/model_convertor.py` on the host with the
#      system python3, whose generated model_config_pb2.py needs
#      protobuf >= 3.20 (`google.protobuf.internal.builder`). On JetPack 6 the
#      system protobuf is too old, so model deploys fail with:
#        "ImportError: cannot import name 'builder' from 'google.protobuf.internal'"
#
# After applying, Docker is restarted and (optionally) Greengrass so the stuck
# LocalServer deployment retries cleanly.
#
# Usage:  sudo ./patch_docker_host_prereqs.sh
# ---------------------------------------------------------------------------
set -u

# Must run as root (modprobe, writing /etc/*, restarting services).
if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run as root. Re-running with sudo..."
    exec sudo "$0" "$@"
fi

WARNINGS=()
add_warning() { echo "⚠️  $1"; WARNINGS+=("$1"); }

ARCH_RAW=$(uname -m)
RESTART_DOCKER=0

echo "=========================================="
echo "DDA edge host prerequisite patch"
echo "Host: $(uname -n)  arch: $ARCH_RAW  kernel: $(uname -r)"
echo "=========================================="
echo ""

# ── 1. iptables kernel modules for Docker bridge networking ────────────────
echo "▶ [1/4] Ensuring Docker iptables kernel modules are loaded..."
DOCKER_IPT_MODULES="iptable_raw iptable_nat iptable_filter ip_tables br_netfilter"
IPT_PERSIST=/etc/modules-load.d/dda-docker-iptables.conf
TMP_MODS=$(mktemp)
RAW_MODULE_OK=0
for mod in $DOCKER_IPT_MODULES; do
    if lsmod 2>/dev/null | awk '{print $1}' | grep -qx "$mod"; then
        echo "   ✓ $mod already loaded"
        echo "$mod" >> "$TMP_MODS"
        [ "$mod" = "iptable_raw" ] && RAW_MODULE_OK=1
    elif modprobe "$mod" 2>/dev/null; then
        echo "   ✓ loaded $mod"
        echo "$mod" >> "$TMP_MODS"
        RESTART_DOCKER=1
        [ "$mod" = "iptable_raw" ] && RAW_MODULE_OK=1
    elif [ "$mod" = "iptable_raw" ]; then
        # Expected on stock JetPack 6 kernels (built without CONFIG_IP_NF_RAW).
        echo "   • iptable_raw not available in this kernel (handled in step 2)"
    else
        add_warning "Could not load kernel module '$mod' (Docker bridge networking may fail on this host)."
    fi
done
# Persist for reboots (best-effort).
if [ -s "$TMP_MODS" ]; then
    if cp "$TMP_MODS" "$IPT_PERSIST" 2>/dev/null; then
        echo "   ✓ persisted modules to $IPT_PERSIST"
    else
        add_warning "Could not write $IPT_PERSIST to persist iptables modules across reboots."
    fi
fi
rm -f "$TMP_MODS" 2>/dev/null || true

# Determine whether the iptables `raw` table is usable at all (module loaded
# above OR compiled built-in to the kernel).
if [ "$RAW_MODULE_OK" -eq 1 ] || iptables -t raw -L -n >/dev/null 2>&1; then
    RAW_TABLE_OK=1
else
    RAW_TABLE_OK=0
fi
echo ""

# ── 2. Docker bridge-networking compatibility on JetPack 6 ─────────────────
# Stock JetPack 6 Tegra kernels are built WITHOUT CONFIG_IP_NF_RAW, so the
# iptables `raw` table does not exist. Docker 28+ added "DIRECT ACCESS
# FILTERING", which programs a DROP rule in that raw table for bridge networks
# with port mappings, so container startup fails with:
#   "can't initialize iptables table `raw': Table does not exist"
# NVIDIA's recommended fix (short of rebuilding the kernel) is to pin Docker to
# 27.5.1, which does not use the raw-table rule. We only do this when the raw
# table is genuinely unavailable AND Docker is >= 28.
echo "▶ [2/4] Checking Docker / iptables-raw compatibility..."
DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "unknown")
DOCKER_MAJOR=$(echo "$DOCKER_VER" | cut -d. -f1)
echo "   Docker server version: $DOCKER_VER"
if [ "$RAW_TABLE_OK" -eq 1 ]; then
    echo "   ✓ iptables 'raw' table is available — no Docker change needed"
elif ! echo "$DOCKER_MAJOR" | grep -qE '^[0-9]+$'; then
    add_warning "Could not determine Docker version; if container networking fails with an iptables 'raw' error, pin Docker to 27.5.1 (see NVIDIA forum)."
elif [ "$DOCKER_MAJOR" -lt 28 ]; then
    echo "   ✓ Docker < 28 does not require the iptables 'raw' table — no change needed"
else
    echo "   ⚠ iptables 'raw' table unavailable and Docker is >= 28."
    echo "     Pinning Docker to 27.5.1 (NVIDIA-recommended fix for JetPack 6)..."
    # Stop Greengrass/Docker workloads cleanly before swapping the engine.
    systemctl stop greengrass 2>/dev/null || true
    if apt-get install -y --allow-downgrades \
            docker-ce=5:27.5.1* docker-ce-cli=5:27.5.1* 2>/dev/null; then
        echo "   ✓ Docker downgraded to 27.5.1"
        # Prevent apt from auto-upgrading Docker back to 28+ (which reintroduces the bug).
        apt-mark hold docker-ce docker-ce-cli >/dev/null 2>&1 && \
            echo "   ✓ Held docker-ce / docker-ce-cli at 27.5.1 (apt-mark hold)" || \
            add_warning "Could not 'apt-mark hold' Docker; a future apt upgrade may bump it back to 28+."
        RESTART_DOCKER=1
    else
        add_warning "Failed to downgrade Docker to 27.5.1. Run manually: sudo apt-get install -y --allow-downgrades docker-ce=5:27.5.1* docker-ce-cli=5:27.5.1*"
    fi
fi
echo ""

# ── 3. NVIDIA Container Runtime registration (Jetson/aarch64 only) ──────────
if [ "$ARCH_RAW" = "aarch64" ]; then
    echo "▶ [3/4] Configuring NVIDIA Container Runtime for Docker (Jetson)..."
    if docker info 2>/dev/null | grep -qi "Runtimes:.*nvidia"; then
        echo "   ✓ nvidia runtime already registered with Docker"
    elif command -v nvidia-ctk >/dev/null 2>&1; then
        if nvidia-ctk runtime configure --runtime=docker --set-as-default; then
            echo "   ✓ nvidia runtime registered via nvidia-ctk"
            RESTART_DOCKER=1
        else
            add_warning "nvidia-ctk runtime configure failed — GPU containers may not start."
        fi
    elif command -v nvidia-container-runtime >/dev/null 2>&1; then
        echo "   nvidia-ctk not found — registering nvidia runtime in daemon.json directly..."
        NVIDIA_RUNTIME_PATH=$(command -v nvidia-container-runtime)
        mkdir -p /etc/docker
        if python3 - "$NVIDIA_RUNTIME_PATH" <<'PYEOF'
import json, os, sys
runtime_path = sys.argv[1]
path = "/etc/docker/daemon.json"
cfg = {}
if os.path.exists(path):
    try:
        with open(path) as f:
            cfg = json.load(f) or {}
    except (ValueError, OSError):
        cfg = {}
runtimes = cfg.setdefault("runtimes", {})
runtimes["nvidia"] = {"path": runtime_path, "runtimeArgs": []}
cfg.setdefault("default-runtime", "nvidia")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print("   Updated /etc/docker/daemon.json")
PYEOF
        then
            RESTART_DOCKER=1
        else
            add_warning "Failed to update /etc/docker/daemon.json with the nvidia runtime."
        fi
    else
        add_warning "Neither nvidia-ctk nor nvidia-container-runtime found — install nvidia-container-runtime for GPU containers."
    fi
    echo ""
else
    echo "▶ [3/4] Skipping NVIDIA runtime registration (not aarch64)."
    echo ""
fi

# ── 4. Host Python deps for the model-conversion Startup script ────────────
# The model component's Greengrass Startup/Shutdown lifecycle runs
#   python3 /aws_dda/model_convertor.py ...      (Startup)
#   python3 /aws_dda/convert_model_cleanup.py ...(Shutdown)
# on the HOST using the *system* python3 (not python3.11). Those scripts need:
#   - protobuf >= 3.20  (generated model_config_pb2.py imports
#       `google.protobuf.internal.builder`, added in 3.20), and
#   - requests          (convert scripts call the LocalServer REST API).
# On JetPack 6 (Ubuntu 22.04) the system python3 is 3.10 and lacks both, so the
# model deploy fails with either:
#   "ImportError: cannot import name 'builder' from 'google.protobuf.internal'"
#   "ModuleNotFoundError: No module named 'requests'"
# Install them for every python interpreter that may run the Startup script
# (system python3 AND python3.11 if present).
echo "▶ [4/4] Ensuring host Python deps for the model-conversion scripts..."
ensure_host_py_deps() {
    local py="$1"
    [ -x "$py" ] || py="$(command -v "$py" 2>/dev/null)"
    [ -n "$py" ] && [ -x "$py" ] || return 0
    local pyname
    pyname="$("$py" -c 'import sys;print(sys.executable)' 2>/dev/null || echo "$py")"

    # protobuf (>=3.20 for the `builder` API).
    if "$py" - <<'PYEOF' >/dev/null 2>&1
from google.protobuf.internal import builder  # noqa: F401
PYEOF
    then
        echo "   ✓ $pyname already has protobuf with builder support"
    else
        echo "   • Installing protobuf>=3.20 for $pyname ..."
        if "$py" -m pip install --upgrade "protobuf>=3.20,<5" >/dev/null 2>&1; then
            echo "   ✓ protobuf installed for $pyname"
        else
            add_warning "Failed to install protobuf for $pyname — model conversion may fail with the 'builder' ImportError."
        fi
    fi

    # requests (used by the convert scripts to call the LocalServer API).
    if "$py" -c 'import requests' >/dev/null 2>&1; then
        echo "   ✓ $pyname already has requests"
    else
        echo "   • Installing requests for $pyname ..."
        if "$py" -m pip install --upgrade requests >/dev/null 2>&1; then
            echo "   ✓ requests installed for $pyname"
        else
            add_warning "Failed to install requests for $pyname — model conversion will fail with 'No module named requests'."
        fi
    fi
}
# Make sure pip exists for the system python3, then patch all candidates.
python3 -m pip --version >/dev/null 2>&1 || apt-get install -y python3-pip >/dev/null 2>&1 || true
ensure_host_py_deps python3
for cand in /usr/local/bin/python3.11 /usr/bin/python3.11; do
    [ -x "$cand" ] && ensure_host_py_deps "$cand"
done
echo ""

# ── Restart Docker so it re-evaluates iptables / picks up the runtime ──────
if [ "$RESTART_DOCKER" -eq 1 ]; then
    echo "▶ Restarting Docker to apply changes..."
    if systemctl restart docker; then
        echo "   ✓ Docker restarted"
    else
        add_warning "Failed to restart Docker — restart it manually: sudo systemctl restart docker"
    fi
    echo ""
fi

# ── Verify ─────────────────────────────────────────────────────────────────
echo "▶ Verification:"
echo "   Docker server version: $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo unknown)"
if iptables -t raw -L -n >/dev/null 2>&1; then
    echo "   iptables 'raw' table: available ✓"
else
    echo "   iptables 'raw' table: NOT available (relying on Docker < 28 bridge networking)"
fi
echo "   Loaded iptables modules:"
lsmod 2>/dev/null | awk '{print $1}' | grep -E '^(iptable_raw|iptable_nat|iptable_filter|ip_tables|br_netfilter)$' | sed 's/^/     - /' || echo "     (none found)"
if [ "$ARCH_RAW" = "aarch64" ]; then
    echo "   Docker runtimes: $(docker info 2>/dev/null | grep -i 'Runtimes:' | sed 's/^ *//')"
fi
echo ""

# ── Offer to restart Greengrass so the stuck deployment retries ────────────
if systemctl list-unit-files 2>/dev/null | grep -q '^greengrass'; then
    echo "▶ Restarting Greengrass so the LocalServer deployment retries with the fix..."
    if systemctl restart greengrass; then
        echo "   ✓ Greengrass restarted"
    else
        add_warning "Failed to restart Greengrass — restart it manually: sudo systemctl restart greengrass"
    fi
    echo ""
fi

echo "=========================================="
if [ "${#WARNINGS[@]}" -eq 0 ]; then
    echo "✅ Patch applied successfully."
else
    echo "⚠️  Patch completed with ${#WARNINGS[@]} warning(s):"
    for w in "${WARNINGS[@]}"; do echo "   - $w"; done
fi
echo "=========================================="
echo ""
echo "Tail the LocalServer logs to confirm the container starts:"
echo "  sudo tail -f /aws_dda/greengrass/v2/logs/aws.edgeml.dda.LocalServer.*.log"
