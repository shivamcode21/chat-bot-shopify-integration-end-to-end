FROM grafana/mcp-grafana:latest AS base

FROM caddy:2-alpine
# Render's runtime refuses to exec setcap'd binaries with "Operation not permitted".
# Strip cap_net_bind_service from caddy. Caddy listens on :10000 here, so the
# capability isn't needed anyway.
RUN apk add --no-cache bash libcap && setcap -r /usr/bin/caddy
COPY --from=base /app/mcp-grafana /app/mcp-grafana
COPY Caddyfile /etc/caddy/Caddyfile
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
EXPOSE 10000
ENTRYPOINT ["/entrypoint.sh"]
