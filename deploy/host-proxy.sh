#!/bin/sh
# Emit only proxy settings, base64-framed for the container-side parser; never eval OS settings.
set -eu
emit() { printf '%s\t' "$1"; printf '%s' "$2" | base64 | tr -d '\r\n'; printf '\n'; }
platform=$(uname -s)
emit platform "$platform"
emit env_https "${https_proxy:-${HTTPS_PROXY:-}}"
emit env_http "${http_proxy:-${HTTP_PROXY:-}}"
emit env_all "${all_proxy:-${ALL_PROXY:-}}"
emit env_no "${no_proxy:-${NO_PROXY:-}}"
case "$platform" in
    Darwin)
        proxy_settings=$(scutil --proxy)
        emit mac "$proxy_settings"
        ;;
    Linux)
        if [ -r /etc/environment ]; then
            emit environment "$(awk '/^[[:space:]]*(https?_proxy|HTTPS?_PROXY|all_proxy|ALL_PROXY|no_proxy|NO_PROXY)=/ { print }' /etc/environment)"
        fi
        if command -v gsettings >/dev/null 2>&1; then
            emit gnome "$(gsettings list-recursively org.gnome.system.proxy 2>/dev/null || true)"
        fi
        reader=''
        if command -v kreadconfig6 >/dev/null 2>&1; then reader=kreadconfig6
        elif command -v kreadconfig5 >/dev/null 2>&1; then reader=kreadconfig5; fi
        if [ -n "$reader" ]; then
            for key in ProxyType httpProxy httpsProxy NoProxyFor; do
                emit "kde_$key" "$("$reader" --file kioslaverc --group 'Proxy Settings' --key "$key")"
            done
        fi
        ;;
    *) emit unsupported 'Unsupported host operating system' ;;
esac
emit complete yes
