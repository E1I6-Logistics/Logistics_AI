#!/usr/bin/env bash

set -u

# Stops the FMS development stack.
# - Docker: Zenoh Router, MQTT Broker
# - Host: FastAPI, Vite, Kev, ROS 2 daemon, zenoh-bridge-ros2dds

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${FMS_COMPOSE_FILE:-$PROJECT_DIR/infra/docker-compose.yml}"
LOG_DIR="${FMS_LOG_DIR:-$PROJECT_DIR/logs}"
KEV_DIR="${FMS_KEV_DIR:-$PROJECT_DIR/../kev}"
KEV_PORT="${FMS_KEV_PORT:-8011}"

ROS_DISTRO="${ROS_DISTRO:-jazzy}"
ROS_SETUP="${FMS_ROS_SETUP:-/opt/ros/$ROS_DISTRO/setup.bash}"

log() {
    printf '[FMS] %s\n' "$*"
}

select_docker_command() {
    if ! command -v docker >/dev/null 2>&1; then
        return 1
    fi

    if docker info >/dev/null 2>&1; then
        DOCKER=(docker)
    elif command -v sudo >/dev/null 2>&1; then
        DOCKER=(sudo docker)
        "${DOCKER[@]}" info >/dev/null 2>&1 || return 1
    else
        return 1
    fi

    "${DOCKER[@]}" compose version >/dev/null 2>&1
}

stop_children() {
    local child
    for child in $(pgrep -P "$1" 2>/dev/null || true); do
        stop_children "$child"
        kill -TERM "$child" >/dev/null 2>&1 || true
    done
}

stop_pid_file() {
    local label="$1"
    local pid_file="$2"
    local expected="$3"

    [ -f "$pid_file" ] || return

    local pid command_line count
    pid="$(tr -cd '0-9' <"$pid_file")"
    if [[ "$pid" =~ ^[1-9][0-9]*$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
        command_line="$(ps -p "$pid" -o args= 2>/dev/null)"
        if [[ "$command_line" == *"$expected"* ]] || \
           [[ "$label" == "Frontend" && "$command_line" == *"npm run dev"* ]]; then
            # Older launchers used npm, which can leave Vite as a child.
            stop_children "$pid"
            kill -TERM "$pid" >/dev/null 2>&1 || true
            for ((count = 0; count < 5; count++)); do
                kill -0 "$pid" >/dev/null 2>&1 || break
                sleep 1
            done
            if kill -0 "$pid" >/dev/null 2>&1; then
                kill -KILL "$pid" >/dev/null 2>&1 || true
            fi
            log "$label: stopped pid $pid"
        else
            log "$label: stale PID file ignored ($pid)"
        fi
    fi
    rm -f "$pid_file"
}

stop_legacy_kev() {
    local pid command_line working_dir count
    while read -r pid; do
        [ -n "$pid" ] || continue
        command_line="$(ps -p "$pid" -o args= 2>/dev/null)"
        working_dir="$(readlink -f "/proc/$pid/cwd" 2>/dev/null)"
        if [[ "$command_line" == *"-m kev.serve"* ]] && \
           [[ "$command_line" == *"--port $KEV_PORT"* ]] && \
           [[ "$working_dir" == "$(readlink -f "$KEV_DIR")" ]]; then
            kill -TERM "$pid" >/dev/null 2>&1 || true
            for ((count = 0; count < 5; count++)); do
                kill -0 "$pid" >/dev/null 2>&1 || break
                sleep 1
            done
            if kill -0 "$pid" >/dev/null 2>&1; then
                kill -KILL "$pid" >/dev/null 2>&1 || true
            fi
            log "Legacy Kev: stop requested for pid $pid"
        fi
    done < <(pgrep -f '[p]ython.*-m kev[.]serve' 2>/dev/null || true)
}

stop_legacy_container() {
    local name="$1"

    if "${DOCKER[@]}" container inspect "$name" >/dev/null 2>&1 && \
       [ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" = "true" ]; then
        "${DOCKER[@]}" stop --time 10 "$name" >/dev/null || true
        log "$name: legacy container stopped"
    fi
}

echo "========================================"
echo " FMS stop"
echo "========================================"

# Host applications in reverse startup order.
stop_pid_file "Kev" "$LOG_DIR/kev.pid" "kev.serve"
stop_legacy_kev
stop_pid_file "Frontend" "$LOG_DIR/frontend.pid" "vite"
stop_pid_file "Backend" "$LOG_DIR/backend.pid" "backend.app.main:app"
stop_pid_file "Zenoh bridge" "$LOG_DIR/zenoh_bridge.pid" "zenoh-bridge-ros2dds"

# Make ros2 available even when stop_fms.sh is run from a plain shell.
if [ -f "$ROS_SETUP" ]; then
    set +u
    # shellcheck disable=SC1090
    source "$ROS_SETUP" >/dev/null 2>&1 || true
    set -u
fi

if command -v ros2 >/dev/null 2>&1; then
    ros2 daemon stop >/dev/null 2>&1 || true
    log "ROS 2 daemon: stop requested"
fi

# Stop the Compose-managed Zenoh Router and MQTT Broker.
if select_docker_command; then
    if [ -f "$COMPOSE_FILE" ]; then
        "${DOCKER[@]}" compose -f "$COMPOSE_FILE" stop \
            zenoh-router \
            mqtt-broker >/dev/null 2>&1 || true

        log "Zenoh Router and MQTT Broker: stop requested"
    fi

    # Migration cleanup: stop containers created by the discarded
    # frontend/backend/zenohd Docker launcher, without deleting them.
    stop_legacy_container "fms-frontend"
    stop_legacy_container "fms-backend"
    stop_legacy_container "fms-zenohd"
else
    log "Docker unavailable; Zenoh Router stop skipped"
fi

echo "========================================"
echo " FMS stop complete"
echo "========================================"
