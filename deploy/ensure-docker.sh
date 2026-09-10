#!/bin/sh
# Sourced by start-local.sh so docker_run can retain a required sudo prefix.

docker_run() { docker "$@"; }

download_docker_file() {
    if command -v curl >/dev/null 2>&1; then
        curl --fail --location --retry 3 "$1" --output "$2"
    elif command -v wget >/dev/null 2>&1; then
        wget -O "$2" "$1"
    else
        echo 'Docker installation requires curl or wget.' >&2
        return 1
    fi
}

ensure_docker() {
    docker_os=$(uname -s)
    if [ "$docker_os" = Darwin ] && [ -d /Applications/Docker.app ]; then
        PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
        export PATH
    fi
    docker_install_required=false
    if ! command -v docker >/dev/null 2>&1; then
        docker_install_required=true
    elif ! docker info >/dev/null 2>&1; then
        case "$docker_os" in
            Darwin) [ -d /Applications/Docker.app ] || docker_install_required=true ;;
            Linux) command -v dockerd >/dev/null 2>&1 || docker_install_required=true ;;
        esac
    fi
    if [ "$docker_install_required" = true ]; then
        echo 'Docker not found. Installing Docker from its official distribution…'
        case "$docker_os" in
            Darwin)
                docker_arch=$(uname -m)
                [ "$docker_arch" != x86_64 ] || docker_arch=amd64
                docker_installer=$(mktemp -d)
                download_docker_file "https://desktop.docker.com/mac/main/$docker_arch/Docker.dmg" "$docker_installer/Docker.dmg"
                mkdir "$docker_installer/mount"
                hdiutil attach "$docker_installer/Docker.dmg" -mountpoint "$docker_installer/mount" -nobrowse
                if ! sudo "$docker_installer/mount/Docker.app/Contents/MacOS/install"; then
                    hdiutil detach "$docker_installer/mount"
                    return 1
                fi
                hdiutil detach "$docker_installer/mount"
                PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
                export PATH
                ;;
            Linux)
                docker_installer=$(mktemp -d)
                download_docker_file https://get.docker.com "$docker_installer/install-docker.sh"
                if [ "$(id -u)" -eq 0 ]; then
                    sh "$docker_installer/install-docker.sh"
                else
                    sudo sh "$docker_installer/install-docker.sh"
                fi
                ;;
            *) echo "Unsupported OS: $docker_os. Use start-local.ps1 on Windows." >&2; return 1 ;;
        esac
    fi
    if ! docker info >/dev/null 2>&1; then
        case "$docker_os" in
            Darwin)
                echo 'Starting Docker Desktop. Complete any first-run prompts in its window.'
                open -a Docker
                ;;
            Linux)
                if [ "$(id -u)" -ne 0 ]; then
                    docker_run() { sudo docker "$@"; }
                fi
                if ! docker_run info >/dev/null 2>&1; then
                    if command -v systemctl >/dev/null 2>&1; then
                        if [ "$(id -u)" -eq 0 ]; then systemctl start docker; else sudo systemctl start docker; fi
                    elif command -v service >/dev/null 2>&1; then
                        if [ "$(id -u)" -eq 0 ]; then service docker start; else sudo service docker start; fi
                    else
                        echo 'Cannot start Docker: this system has no systemctl or service manager.' >&2
                        return 1
                    fi
                fi
                ;;
        esac
    fi
    docker_attempt=0
    until docker_run info >/dev/null 2>&1; do
        docker_attempt=$((docker_attempt + 1))
        if [ "$docker_attempt" -ge 150 ]; then
            echo 'Docker is not ready. Complete Desktop setup or the required restart, then rerun this script.' >&2
            return 1
        fi
        sleep 2
    done
    if ! docker_run compose version >/dev/null 2>&1; then
        echo 'Docker Compose is missing; installing the official Compose CLI plugin…'
        if [ "$docker_os" != Linux ]; then
            echo 'Repair Docker Desktop to restore its Compose plugin.' >&2
            return 1
        fi
        docker_arch=$(uname -m)
        docker_compose_tmp=$(mktemp -d)
        download_docker_file "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$docker_arch" "$docker_compose_tmp/docker-compose"
        if [ "$(id -u)" -eq 0 ]; then
            install -D -m 0755 "$docker_compose_tmp/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
        else
            sudo install -D -m 0755 "$docker_compose_tmp/docker-compose" /usr/local/lib/docker/cli-plugins/docker-compose
        fi
        docker_run compose version
    fi
}
