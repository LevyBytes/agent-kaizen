# Kaizen hub: tursodb sync-server leg (see docker-compose.hub.yml).
#
# Self-contained build — no external registry image is assumed for tursodb. Before building, place the
# tursodb release binary for THIS host's architecture next to this Dockerfile as ./tursodb
# (Linux x86_64 or ARM64 — the ARM64 artifact exists, which is what makes a GB10-class hub possible).
FROM debian:bookworm-slim@sha256:7b140f374b289a7c2befc338f42ebe6441b7ea838a042bbd5acbfca6ec875818
COPY tursodb /usr/local/bin/tursodb
COPY hub-sync-entrypoint.sh /usr/local/bin/hub-sync-entrypoint
RUN chmod +x /usr/local/bin/tursodb /usr/local/bin/hub-sync-entrypoint \
    && /usr/local/bin/tursodb --help >/dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 kaizen-hub \
    && useradd --system --uid 10001 --gid kaizen-hub --no-create-home kaizen-hub \
    && mkdir -p /data \
    && chown kaizen-hub:kaizen-hub /data
VOLUME /data
EXPOSE 8080
# hub.db lives on the /data volume; a container restart with the same volume preserves the ledger.
# NEVER seed /data from an old snapshot; read the rewind warning in README.md before restoring hub state.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 CMD ["sh", "-c", "tr '\\0' ' ' </proc/1/cmdline | grep -q '^/usr/local/bin/tursodb '"]
ENTRYPOINT ["/usr/local/bin/hub-sync-entrypoint"]
CMD ["/usr/local/bin/tursodb", "/data/hub.db", "--sync-server", "0.0.0.0:8080"]
