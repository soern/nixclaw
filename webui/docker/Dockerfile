# NixClaw web interface: Caddy serves webui/ and proxies /api/* to the nixclaw
# web API. Config: config/webinterface.yaml (fallback: .example), see entrypoint.sh.
FROM caddy:2-alpine
# Alpine's mikefarah yq is packaged as "yq-go" (binary: yq).
RUN apk add --no-cache yq-go openssl
COPY entrypoint.sh /usr/local/bin/nixclaw-webui-entrypoint
RUN chmod 0755 /usr/local/bin/nixclaw-webui-entrypoint
ENTRYPOINT ["/usr/local/bin/nixclaw-webui-entrypoint"]
