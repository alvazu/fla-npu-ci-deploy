#!/usr/bin/env bash
# Source this file, then use proxyon/proxyoff. No service is changed while sourcing.
_fla_proxy_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
export PROXY_HOST=127.0.0.1
export PROXY_RUNTIME_CONFIG="${_fla_proxy_root}/clashctl/data/runtime.yaml"

_fla_proxy_owner_uid() {
    stat -c '%u' "${_fla_proxy_root}/clashctl"
}

_fla_proxy_as_owner() {
    local owner_uid
    owner_uid=$(_fla_proxy_owner_uid) || return
    if [ "$owner_uid" = "$(id -u)" ]; then
        "$@"
    else
        # CI may run as root while clashctl belongs to the ordinary deployment
        # user. Read only the required ports and manage that user's existing unit.
        sudo -n -u "#${owner_uid}" -- "$@"
    fi
}

clashctl() {
    # The official installer exposes a shell function, not a standalone binary.
    # Keep its private configuration and shell side effects in the owner's child
    # process, including when root/cann uses an ubuntu-owned deployment.
    _fla_proxy_as_owner env CLASHCTL_HOME="${_fla_proxy_root}/clashctl" bash -c '
        source "$CLASHCTL_HOME/scripts/cmd/clashctl.sh" || exit
        clashctl "$@"
    ' fla-ci-clashctl "$@"
}

_refresh_proxy_ports() {
    _fla_proxy_as_owner test -r "$PROXY_RUNTIME_CONFIG" || {
        printf '%s\n' 'Proxy config missing; rerun deploy.sh --proxy-only' >&2
        return 1
    }
    local key value http_port= mixed_port= socks_port=
    for key in port mixed-port socks-port; do
        value=$(_fla_proxy_as_owner sed -n "/^${key}:[[:space:]]*[0-9]/ { s/^${key}:[[:space:]]*\([0-9][0-9]*\).*/\1/; p; q; }" "$PROXY_RUNTIME_CONFIG") || return
        case "$value" in ''|*[!0-9]*) continue ;; esac
        [ "${#value}" -le 5 ] && [ "$value" -gt 0 ] && [ "$value" -le 65535 ] || continue
        case "$key" in
            port) http_port="$value" ;;
            mixed-port) mixed_port="$value" ;;
            socks-port) socks_port="$value" ;;
        esac
    done
    PROXY_HTTP_PORT="${http_port:-$mixed_port}"
    PROXY_MIXED_PORT="$mixed_port"
    PROXY_SOCKS_PORT="${socks_port:-$mixed_port}"
    if [ -z "$PROXY_HTTP_PORT" ] || [ -z "$PROXY_SOCKS_PORT" ]; then
        printf '%s\n' 'Proxy config needs enabled HTTP/mixed and SOCKS/mixed ports; repair clashctl config' >&2
        return 1
    fi
    export PROXY_HTTP_PORT PROXY_MIXED_PORT PROXY_SOCKS_PORT
}

_fla_proxy_systemctl() {
    local owner_uid runtime
    owner_uid=$(_fla_proxy_owner_uid) || return
    runtime="/run/user/${owner_uid}"
    _fla_proxy_as_owner env XDG_RUNTIME_DIR="$runtime" DBUS_SESSION_BUS_ADDRESS="unix:path=${runtime}/bus" \
        systemctl --user "$@"
}

_fla_proxy_owned_service() {
    local command
    command=$(_fla_proxy_systemctl show mihomo.service --property=ExecStart --value) || return
    case "$command" in
        *"${_fla_proxy_root}/clashctl/bin/mihomo"*) return 0 ;;
        *) printf '%s\n' 'mihomo.service belongs to another installation; refusing to manage it' >&2; return 1 ;;
    esac
}

_fla_proxy_listening() {
    timeout 2 bash -c 'exec 3<>"/dev/tcp/$1/$2"' proxy-listener "$PROXY_HOST" "$PROXY_HTTP_PORT" 2>/dev/null
}

_set_proxy_env() {
    export http_proxy="http://${PROXY_HOST}:${PROXY_HTTP_PORT}"
    export https_proxy="$http_proxy" HTTP_PROXY="$http_proxy" HTTPS_PROXY="$http_proxy"
    export all_proxy="socks5h://${PROXY_HOST}:${PROXY_SOCKS_PORT}"
    export ALL_PROXY="$all_proxy"
    export no_proxy="127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"
    export NO_PROXY="$no_proxy" GIT_HTTP_PROXY="$http_proxy" GIT_HTTPS_PROXY="$http_proxy"
}

proxyon() {
    # An unsuccessful activation must not leave a stale inherited proxy enabled.
    # This only affects the caller's shell, never an existing runner process.
    proxyoff >/dev/null
    _refresh_proxy_ports && _fla_proxy_owned_service || return
    # An active unit can serve an existing runner job. Start only; never restart it.
    _fla_proxy_systemctl is-active --quiet mihomo.service || _fla_proxy_systemctl start mihomo.service || return
    local attempt
    for attempt in {1..20}; do
        if _fla_proxy_listening; then
            _set_proxy_env
            printf 'Proxy enabled: HTTP(S)=%s, SOCKS=%s\n' "$http_proxy" "$all_proxy"
            return 0
        fi
        sleep 0.25
    done
    printf '%s\n' 'Proxy service has no HTTP listener; inspect journalctl --user -u mihomo.service' >&2
    return 1
}

proxycheck() {
    # Read-only, including when the service is stopped. Explicit flags ignore stale
    # inherited proxy/no_proxy variables and ~/.curlrc; TLS verification stays on.
    _refresh_proxy_ports && _fla_proxy_owned_service || return
    _fla_proxy_systemctl is-active --quiet mihomo.service && _fla_proxy_listening || {
        printf '%s\n' 'Proxy service/listener unavailable; run proxyon first' >&2
        return 1
    }
    if ! curl --disable --fail --location --silent --show-error --connect-timeout 10 --max-time 30 \
        --proxy "http://${PROXY_HOST}:${PROXY_HTTP_PORT}" --noproxy '' \
        --output /dev/null https://github.com/; then
        printf '%s\n' 'Proxy cannot reach GitHub over HTTPS; inspect subscription and selected node with clashctl' >&2
        return 1
    fi
    printf '%s\n' 'Proxy HTTPS connectivity to GitHub is available'
}

proxyoff() {
    unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY
    unset no_proxy NO_PROXY GIT_HTTP_PROXY GIT_HTTPS_PROXY
    printf '%s\n' 'Proxy environment disabled; the user proxy service remains running'
}
