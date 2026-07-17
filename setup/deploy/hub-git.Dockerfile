# Kaizen hub: bare git remote leg (see docker-compose.hub.yml).
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/* && mkdir -p /repos
VOLUME /repos
EXPOSE 9418
# git daemon serves every bare repo under /repos. receive-pack is enabled because the KAIZEN HUB is a push target for kz/<task>/<node> branches. Reachability is set by the host interface that compose publishes 9418 on: default 127.0.0.1, or explicitly the host Tailscale address for tailnet service; git daemon itself has no auth.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["sh", "-c", "tr '\\0' ' ' </proc/1/cmdline | grep -q '^git daemon '"]
ENTRYPOINT ["git", "daemon", "--reuseaddr", "--base-path=/repos", "--export-all", "--enable=receive-pack", "--verbose"]
