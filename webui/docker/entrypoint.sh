#!/bin/sh
# Render a Caddyfile from config/webinterface.yaml and run Caddy.
#   REPO_DIR  - where the repo's config/ is mounted (default /nixclaw); relative
#               paths in the yaml (cert_file, key_file) resolve against it.
#   WEBUI_DIR - static frontend (default /srv/webui).
set -eu

REPO_DIR="${REPO_DIR:-/nixclaw}"
WEBUI_DIR="${WEBUI_DIR:-/srv/webui}"
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

CFG="$REPO_DIR/config/webinterface.yaml"
if [ ! -s "$CFG" ]; then
    CFG="$REPO_DIR/config/webinterface.yaml.example"
    echo "webui: config/webinterface.yaml missing, using the .example"
fi
[ -s "$CFG" ] || { echo "webui: no config found at $CFG" >&2; exit 1; }

y() { yq -r "$1" "$CFG"; }
abs() { case "$1" in /*) echo "$1" ;; *) echo "$REPO_DIR/$1" ;; esac; }

HTTP_ON=$(y '.http.enabled // false')
HTTP_PORT=$(y '.http.port // 8090')
HTTPS_ON=$(y '.https.enabled // false')
HTTPS_PORT=$(y '.https.port // 8443')
CERT=$(abs "$(y '.https.cert_file // "config/ssl/cert.pem"')")
KEY=$(abs "$(y '.https.key_file // "config/ssl/key.pem"')")
DAYS=$(y '.https.self_signed.days // 825')
UPSTREAM=$(y '.api_upstream // "127.0.0.1:3003"')
MAX_MB=$(y '.max_upload_mb // 50')
BODY_MB=$((MAX_MB + 2))   # headroom for multipart framing / form fields; MiB like the API

if [ "$HTTP_ON" != "true" ] && [ "$HTTPS_ON" != "true" ]; then
    echo "webui: neither http nor https is enabled in $CFG" >&2
    exit 1
fi

if [ "$HTTPS_ON" = "true" ] && { [ ! -s "$CERT" ] || [ ! -s "$KEY" ]; }; then
    SAN=""
    for h in $(y '.https.self_signed.hostnames // ["localhost"] | .[]'); do
        case "$h" in
            *:*) t=IP ;;                    # IPv6 literal
            *[!0-9.]*) t=DNS ;;
            *) t=IP ;;                      # IPv4 literal
        esac
        SAN="${SAN:+$SAN,}$t:$h"
    done
    CN=$(y '.https.self_signed.hostnames[0] // "localhost"')
    echo "webui: generating self-signed certificate ($SAN, $DAYS days) -> $CERT"
    if ! mkdir -p "$(dirname "$CERT")" "$(dirname "$KEY")" ||
       ! openssl req -x509 -newkey rsa:2048 -nodes -sha256 -days "$DAYS" \
            -subj "/CN=$CN" -addext "subjectAltName=$SAN" \
            -keyout "$KEY" -out "$CERT"; then
        echo "webui: ERROR: generating the self-signed certificate failed (cert=$CERT key=$KEY)." >&2
        echo "webui: cert_file and key_file must be inside config/ssl/, the only writable mount" >&2
        echo "webui: (or provide both files yourself, or disable https in config/webinterface.yaml)." >&2
        exit 1
    fi
    chmod 600 "$KEY"
    # Docker creates a missing bind-mount source as root: hand dir + files to the
    # owner of the repo's config/ so the host user can manage them.
    owner=$(stat -c %u:%g "$REPO_DIR/config")
    chown "$owner" "$(dirname "$CERT")" "$(dirname "$KEY")" "$CERT" "$KEY" 2>/dev/null || true
fi

CSP="default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'"

{
cat <<CADDY
{
	admin off
	auto_https off
}

(nixclaw) {
	handle /api/* {
		request_body {
			max_size ${BODY_MB}MiB
		}
		# flush_interval -1: never buffer (SSE). Caddy sets X-Forwarded-For /
		# X-Forwarded-Proto itself and drops client-supplied ones (no trusted_proxies).
		reverse_proxy ${UPSTREAM} {
			flush_interval -1
		}
	}
	handle {
		header {
			Content-Security-Policy "${CSP}"
			X-Content-Type-Options nosniff
			Referrer-Policy no-referrer
			X-Frame-Options DENY
			Cache-Control no-cache
		}
		root * ${WEBUI_DIR}
		file_server {
			hide ${WEBUI_DIR}/docker
		}
	}
}
CADDY
if [ "$HTTP_ON" = "true" ]; then
    printf '\nhttp://:%s {\n\timport nixclaw\n}\n' "$HTTP_PORT"
fi
if [ "$HTTPS_ON" = "true" ]; then
    printf '\nhttps://:%s {\n\ttls %s %s\n\timport nixclaw\n}\n' "$HTTPS_PORT" "$CERT" "$KEY"
fi
} > "$CADDYFILE"

caddy fmt --overwrite "$CADDYFILE" >/dev/null 2>&1 || true
echo "webui: http=$HTTP_ON:$HTTP_PORT https=$HTTPS_ON:$HTTPS_PORT api=$UPSTREAM max_upload=${MAX_MB}MiB body_limit=${BODY_MB}MiB"
exec caddy run --config "$CADDYFILE" --adapter caddyfile
