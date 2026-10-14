#!/usr/bin/env bash
set -euo pipefail

# Install the Main PC or Robot PC environment with one command.
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROLE="${1:-main}"
ROS_DISTRO="jazzy"
NODE_VERSION="20.20.2"
VENV_DIR="${FMS_VENV_DIR:-$HOME/venv/robot}"
ROS_WS_DIR="${FMS_ROS_WS_DIR:-$PROJECT_DIR/robots_ws/Logistics_AMR}"
ROS_WS_BRANCH="dev"
KEV_DIR="${FMS_KEV_DIR:-$PROJECT_DIR/../kev}"

if [[ "$ROLE" != "main" && "$ROLE" != "robot" || $# -gt 1 ]]; then
    echo "Usage: bash scripts/setup.sh [main|robot]" >&2
    exit 2
fi
if ! grep -q 'VERSION_ID="24.04"' /etc/os-release; then
    echo "[ERROR] Ubuntu 24.04 is required." >&2
    exit 1
fi
case "$(uname -m)" in
    x86_64|aarch64) ;;
    *) echo "[ERROR] Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

# Install packages shared by the Main PC and Robot PC.
install_base() {
    sudo apt update
    sudo apt install -y software-properties-common curl wget git ca-certificates gnupg lsb-release locales build-essential cmake gcc g++ python3 python3-pip python3-venv python3-dev
    sudo locale-gen en_US en_US.UTF-8
    sudo update-locale LC_ALL=en_US.UTF-8 LANG=en_US.UTF-8
    sudo add-apt-repository universe -y
    if ! dpkg -s ros2-apt-source >/dev/null 2>&1; then
        local version codename
        version="$(curl -fsSL https://api.github.com/repos/ros-infrastructure/ros-apt-source/releases/latest | awk -F'"' '/tag_name/ {print $4; exit}')"
        [ -n "$version" ] || { echo "[ERROR] Cannot determine ros2-apt-source version" >&2; return 1; }
        codename="$(. /etc/os-release; echo "$VERSION_CODENAME")"
        curl -fsSL -o /tmp/ros2-apt-source.deb "https://github.com/ros-infrastructure/ros-apt-source/releases/download/${version}/ros2-apt-source_${version}.${codename}_all.deb"
        sudo dpkg -i /tmp/ros2-apt-source.deb
    fi
    sudo apt update
    sudo apt install -y ros-jazzy-desktop ros-jazzy-navigation2 ros-jazzy-nav2-bringup ros-jazzy-robot-localization ros-jazzy-tf2-tools ros-jazzy-slam-toolbox ros-jazzy-rosbridge-server ros-jazzy-teleop-twist-keyboard ros-jazzy-cv-bridge ros-jazzy-ros-gz-sim ros-jazzy-ros-gz-bridge ros-jazzy-rmw-cyclonedds-cpp ros-jazzy-rmw-fastrtps-cpp ros-jazzy-turtlebot3-msgs python3-colcon-common-extensions python3-rosdep python3-vcstool
    # ROS setup scripts may reference unset variables.
    set +u
    source "/opt/ros/$ROS_DISTRO/setup.bash"
    set -u
    if [ ! -f /etc/ros/rosdep/sources.list.d/20-default.list ]; then sudo rosdep init; fi
    rosdep update
}

# Add the appropriate shell configuration without duplicating the source line.
install_shell_config() {
    local config="$1" target="$2"
    cp "$config" "$target"
    touch "$HOME/.bashrc"
    if ! grep -Fq "source $target" "$HOME/.bashrc"; then
        printf '\n# Logistics_FMS environment\nsource %s\n' "$target" >> "$HOME/.bashrc"
    fi
}

# Install ROS interfaces used by the backend, using an existing checkout when present.
install_ros_workspace() {
    if [ ! -d "$ROS_WS_DIR" ]; then
        mkdir -p "$(dirname "$ROS_WS_DIR")"
        git clone --branch "$ROS_WS_BRANCH" https://github.com/E1I6-Logistics/Logistics_AMR.git "$ROS_WS_DIR"
    fi
    [ -d "$ROS_WS_DIR/src" ] || { echo "[ERROR] ROS workspace source missing: $ROS_WS_DIR/src" >&2; return 1; }
    if [ -e "$ROS_WS_DIR/.git" ]; then
        local current_branch
        current_branch="$(git -C "$ROS_WS_DIR" branch --show-current)"
        [ "$current_branch" = "$ROS_WS_BRANCH" ] || { echo "[ERROR] Logistics_AMR must use branch $ROS_WS_BRANCH (current: ${current_branch:-detached HEAD})." >&2; return 1; }
    fi
    (cd "$ROS_WS_DIR" && rosdep install --from-paths src --ignore-src -r -y && colcon build --symlink-install)
}

# Install the dashboard, backend, AI packages, Kev server, and Main PC transport.
install_main() {
    sudo apt install -y docker.io docker-compose-v2
    sudo systemctl enable --now docker
    if ! id -nG "$USER" | grep -qw docker; then
        sudo usermod -aG docker "$USER"
        echo "[INFO] Log out and log in again to use Docker without sudo."
    fi

    export NVM_DIR="$HOME/.nvm"
    if [ ! -s "$NVM_DIR/nvm.sh" ]; then curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.3/install.sh | bash; fi
    set +u
    source "$NVM_DIR/nvm.sh"
    nvm install "$NODE_VERSION"
    nvm alias default "$NODE_VERSION"
    nvm use "$NODE_VERSION"
    set -u

    mkdir -p "$(dirname "$VENV_DIR")"
    if [ ! -f "$VENV_DIR/bin/python" ]; then python3 -m venv --system-site-packages "$VENV_DIR"; fi
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/backend/requirements.txt" -r "$PROJECT_DIR/backend/simulation/requirements.txt"
    if [ "${FMS_KEV_ENABLED:-1}" = "1" ]; then
        if [ ! -d "$KEV_DIR/kev" ]; then git clone https://github.com/jaredpalmer/kev.git "$KEV_DIR"; fi
        [ -f "$KEV_DIR/pyproject.toml" ] || { echo "[ERROR] Kev source missing: $KEV_DIR" >&2; return 1; }
        "$VENV_DIR/bin/python" -m pip install "$KEV_DIR[serve]"
        # ROS system packages are visible in this venv, so replace NumPy 1.x binary extensions for Kev's NumPy 2.x.
        "$VENV_DIR/bin/python" -m pip install --upgrade "numexpr>=2.10.0" "Bottleneck>=1.4.0"
    fi

    bash "$PROJECT_DIR/scripts/main/install_main.sh"
    install_ros_workspace
    (cd "$PROJECT_DIR/frontend" && if [ -f package-lock.json ]; then npm ci; else npm install; fi)
    if [ ! -f "$PROJECT_DIR/.env" ]; then cp "$PROJECT_DIR/.env.example" "$PROJECT_DIR/.env"; fi
    if [ ! -f "$PROJECT_DIR/infra/docker-compose.yml" ]; then cp "$PROJECT_DIR/infra/docker-compose.example.yml" "$PROJECT_DIR/infra/docker-compose.yml"; fi
    install_shell_config "$PROJECT_DIR/scripts/main/bashrc_main.conf" "$HOME/.axbashrc"
    chmod +x "$PROJECT_DIR/start_fms.sh" "$PROJECT_DIR/stop_fms.sh"
}

# Install only the ROS/Zenoh packages and shell settings needed on a Robot PC.
install_robot() {
    bash "$PROJECT_DIR/scripts/robot/install_robot.sh"
    install_shell_config "$PROJECT_DIR/scripts/robot/bashrc_robot.conf" "$HOME/.axrobotbashrc"
}

# Confirm the bridge installed from the Eclipse Zenoh apt repository works.
verify_zenoh_bridge() {
    echo "[SETUP] Checking Zenoh Bridge version and options"
    command -v zenoh-bridge-ros2dds >/dev/null 2>&1 || { echo "[ERROR] zenoh-bridge-ros2dds not found." >&2; return 1; }
    zenoh-bridge-ros2dds --version
    zenoh-bridge-ros2dds --help >/dev/null
    echo "[SETUP] Zenoh Bridge options check passed"
}

echo "[SETUP] Installing $ROLE environment in $PROJECT_DIR"
install_base
if [ "$ROLE" = "main" ]; then install_main; else install_robot; fi
verify_zenoh_bridge
echo "[SETUP] Checking installed $ROLE environment"
bash "$PROJECT_DIR/scripts/verify_env.sh" "$ROLE"
echo "[SETUP] Complete. Open a new terminal to load the shell environment."
