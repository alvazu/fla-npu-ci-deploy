#!/usr/bin/env bash
# Source this file, then use proxyon/proxyoff. Derived from the supplied vpn-proxy.sh.
_fla_proxy_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PROXY_HOST=127.0.0.1
export PROXY_HTTP_PORT=7899
export PROXY_MIXED_PORT=7898
export PROXY_SOCKS_PORT=7891
export PROXY_RUNTIME_CONFIG="${_fla_proxy_root}/clashctl/data/runtime.yaml"

_refresh_proxy_ports() {
    [ -r "$PROXY_RUNTIME_CONFIG" ] || return 1
    local key value
    for key in port mixed-port socks-port; do
        value=$(sed -n "s/^${key}:[[:space:]]*\([0-9][0-9]*\).*/\1/p" "$PROXY_RUNTIME_CONFIG" | head -n 1)
        [ -z "$value" ] && continue
        case "$key" in
            port) PROXY_HTTP_PORT="$value" ;;
            mixed-port) PROXY_MIXED_PORT="$value" ;;
            socks-port) PROXY_SOCKS_PORT="$value" ;;
        esac
    done
    export PROXY_HTTP_PORT PROXY_MIXED_PORT PROXY_SOCKS_PORT
}

_set_proxy_env() {
    _refresh_proxy_ports || return
    export http_proxy="http://${PROXY_HOST}:${PROXY_HTTP_PORT}"
    export https_proxy="$http_proxy" HTTP_PROXY="$http_proxy" HTTPS_PROXY="$http_proxy"
    export all_proxy="socks5h://${PROXY_HOST}:${PROXY_SOCKS_PORT:-$PROXY_MIXED_PORT}"
    export ALL_PROXY="$all_proxy"
    export no_proxy="127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    export NO_PROXY="$no_proxy" GIT_HTTP_PROXY="$http_proxy" GIT_HTTPS_PROXY="$http_proxy"
}

proxyon() {
    # Starting an already active unit is harmless. Never pgrep/pkill another user's proxy.
    systemctl --user is-active --quiet mihomo.service || systemctl --user start mihomo.service || return
    _set_proxy_env || return
    printf 'Proxy enabled: HTTP(S)=%s, SOCKS=%s\n' "$http_proxy" "$all_proxy"
}

proxyoff() {
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
    unset no_proxy NO_PROXY GIT_HTTP_PROXY GIT_HTTPS_PROXY
    printf '%s\n' 'Proxy environment disabled; the user proxy service remains running'
}
