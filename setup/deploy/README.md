# Deploy assets (fleet hub)

Owner-run bring-up for the optional multi-machine fleet hub. Single-node use needs none of this — the hub exists so fleet nodes can share one ledger and exchange work over Tailscale.

By design this project ships NO machine-level persistence assets: no scheduled tasks, no services, no systemd units, no autoruns. Every process here — the per-workspace supervisor daemon and both hub legs — is started per-session by explicit user action and stops when you stop it. The daemon on any node runs with `python kaizen.py daemon run` in a visible terminal, or via the VS Code extension's Start Daemon action.

## Native bring-up (primary — no Docker required)

The hub is two ordinary processes on the hub box, each in its own visible terminal (or tmux pane). Bind both to the host's **Tailscale** address — never `0.0.0.0`; the tailnet ACL is the boundary. The `/path/to/...` values below are POSIX placeholders; use equivalent native paths on Windows.

Sync leg — the fleet ledger sync point (single native `tursodb` binary; Linux x86_64/ARM64 and Windows artifacts exist):

```text
tursodb /path/to/hub-data/hub.db --sync-server <tailscale-addr>:8080
```

Git leg — the bare-repo push target for `kz/<task>/<node>` branches:

```text
git daemon --reuseaddr --base-path=/path/to/hub-repos --export-all --enable=receive-pack --verbose --listen=<tailscale-addr>
```

With no explicit `--port`, git daemon listens on its standard port, 9418.

`hub.db` and the repos directory are the durable state. NEVER restore them from an old snapshot without reading the hub-restore rewind hazard in the hub runbook — node-side watermarks refuse the regression.

## Docker compose (optional convenience)

The same two legs packaged as containers, for a hub box where you prefer Docker: drop the `tursodb` release binary next to the compose file, then explicitly run `docker compose -f docker-compose.hub.yml up -d` — idempotent: running-and-unchanged is a no-op (`config -q` validates without starting; `docker compose down` removes). The compose file intentionally defines no restart policy, so Docker never starts or restarts the hub without that owner action. The build runs `tursodb --help` to reject a corrupt or wrong-architecture binary before startup. The sync entrypoint migrates an existing `/data` volume to the fixed non-root UID/GID 10001 before dropping privileges; make a volume backup before the first hardened-image start if host-side ownership matters. Defaults bind 127.0.0.1; to serve the tailnet, edit the port mappings to the host's Tailscale address.

Both Dockerfiles pin the official multi-platform `debian:bookworm-slim` index digest. Refresh both pins together after verifying the current index digest on Docker Hub, then rebuild both services in one compose change.

## Network posture

- Everything binds locally or to the tailnet, never `0.0.0.0`. Peers address the hub by MagicDNS name.
- The git daemon has receive-pack enabled and no auth of its own: the tailnet ACL is the boundary. Service nodes carry service tags; human devices keep user identity.

## Observability

Once a node is up: `GET /v1/probe` and `GET /v1/health` on the control service (unsigned, read-only), `kaizen.py R0` fleet section (advisory metrics: heartbeat ages, sync staleness, open lease conflicts, orphan sweeps, dispatch latency), and the daemon-only `fleet/metrics` / `fleet/stats` loopback ops. OTEL export is deliberately not built (owner-gated future work).
