#!/usr/bin/env bash
set -uo pipefail

# Verify the packages installed for the selected computer role.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE="${1:-main}"
VENV_DIR="${FMS_VENV_DIR:-$HOME/venv/robot}"
ROS_WS_SETUP="${FMS_ROS_WS_SETUP:-$PROJECT_DIR/robots_ws/Logistics_AMR/install/setup.bash}"
FAIL=0

if [[ "$ROLE" != "main" && "$ROLE" != "robot" || $# -gt 1 ]]; then
    echo "Usage: bash scripts/verify_env.sh [main|robot]" >&2
    exit 2
fi

# Record each result while checking the rest of the environment.
ok() { echo "[OK] $1"; }
fail() { echo "[FAIL] $1"; FAIL=1; }
check_command() {
    if command -v "$1" >/dev/null 2>&1; then ok "$1"; else fail "$1"; fi
}
check_ros_package() {
    if command -v ros2 >/dev/null 2>&1 && ros2 pkg prefix "$1" >/dev/null 2>&1; then ok "$1"; else fail "$1"; fi
}

echo "[VERIFY] Role: $ROLE | Project: $PROJECT_DIR"
if grep -q 'VERSION_ID="24.04"' /etc/os-release; then ok "Ubuntu 24.04"; else fail "Ubuntu 24.04"; fi
if [ -f /opt/ros/jazzy/setup.bash ]; then
    set +u
    source /opt/ros/jazzy/setup.bash
    if [ "$ROLE" = "main" ] && [ -f "$ROS_WS_SETUP" ]; then source "$ROS_WS_SETUP"; fi
    set -u
    ok "ROS 2 Jazzy"
else
    fail "ROS 2 Jazzy"
fi

for package in nav2_bringup nav2_route robot_localization rmw_cyclonedds_cpp rmw_fastrtps_cpp turtlebot3_msgs; do
    check_ros_package "$package"
done
if [ "$ROLE" = "main" ]; then
    if [ -f "$ROS_WS_SETUP" ]; then ok "Logistics_AMR workspace"; else fail "Logistics_AMR workspace: $ROS_WS_SETUP"; fi
    check_ros_package turtlebot3_my_msg
fi

check_command zenoh-bridge-ros2dds
if command -v zenoh-bridge-ros2dds >/dev/null 2>&1; then zenoh-bridge-ros2dds --version || fail "Zenoh bridge version"; fi

if [ "$ROLE" = "main" ]; then
    check_command zenohd
    check_command docker
    if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then ok "Docker Compose"; else fail "Docker Compose"; fi
    export NVM_DIR="$HOME/.nvm"
    if [ -s "$NVM_DIR/nvm.sh" ]; then
        set +u
        source "$NVM_DIR/nvm.sh"
        set -u
    fi
    check_command node
    check_command npm
    if [ -d "$PROJECT_DIR/frontend/node_modules" ]; then ok "Frontend dependencies"; else fail "Frontend dependencies"; fi
    if [ -f "$PROJECT_DIR/.env" ]; then ok "Root .env"; else fail "Root .env"; fi
    if [ -f "$PROJECT_DIR/infra/docker-compose.yml" ]; then ok "Docker Compose config"; else fail "Docker Compose config"; fi
    if [ -x "$VENV_DIR/bin/python" ]; then
        ok "Python virtual environment"
        "$VENV_DIR/bin/python" - <<'PY'
import importlib
import os
import sys

modules = ["fastapi", "uvicorn", "zenoh", "yaml", "PIL", "pydantic", "rclpy", "turtlebot3_my_msg", "laya", "anthropic", "ollama"]
if os.getenv("FMS_KEV_ENABLED", "1") == "1":
    modules.extend(["numexpr", "bottleneck", "kev.serve"])
missing = []
for name in modules:
    try:
        importlib.import_module(name)
    except Exception as error:
        missing.append(f"{name}: {error}")
if os.getenv("FMS_KEV_ENABLED", "1") == "1":
    try:
        import brotli
        compressed = brotli.compress(b"fms")
        decoded = brotli.Decompressor().process(compressed, output_buffer_limit=1024)
        if decoded != b"fms":
            raise RuntimeError("Brotli decompression returned unexpected data")
    except Exception as error:
        missing.append(f"Kev download Brotli decoder: {error}")
for item in missing:
    print(f"[FAIL] Python import {item}")
if not missing:
    print("[OK] Python, ROS, and AI imports")
sys.exit(bool(missing))
PY
        if [ $? -ne 0 ]; then FAIL=1; fi
    else
        fail "Python virtual environment: $VENV_DIR"
    fi
fi

if [ "$FAIL" -eq 0 ]; then echo "[VERIFY] PASS ($ROLE)"; else echo "[VERIFY] FAIL ($ROLE)"; fi
exit "$FAIL"
