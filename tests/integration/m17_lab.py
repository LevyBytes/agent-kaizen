r"""M17 two-node metrics lab (the §5.3 M17 exit: "metrics visible ... on a two-node lab"): two SHIPPED
FleetStore replicas over a real `tursodb --sync-server` generate every metric class with real traffic
(heartbeats, a dispatched run, a contested-claim conflict, an orphan sweep), then `fleet_metrics` on the
SURVIVOR shows them all, and the control service `/v1/health` answers unsigned over a real socket.

Run: python tests/integration/m17_lab.py --tursodb-exe <path>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

HOST = "127.0.0.1"

NODE_A = "nlab0000000m17a"
NODE_B = "nlab0000000m17b"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def unused_port() -> int:
    """Reserve and release an ephemeral loopback port for the lab server."""
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def wait_port(host: str, port: int, proc: subprocess.Popen[bytes], timeout: float = 20.0) -> bool:
    """Return true when this still-running server accepts a loopback connection before timeout."""
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def main(argv: list[str] | None = None) -> int:
    """Entry point returning exit code (0 PASS / 1 FAIL / 2 SKIP when tursodb missing); side effects — mutates KAIZEN_REPO_ROOT, inits a scratch plane, spawns tursodb + a control service, prints one JSON verdict to stdout."""
    parser = argparse.ArgumentParser(description="Run the M17 two-node metrics lab.")
    parser.add_argument("--tursodb-exe", required=True)
    args = parser.parse_args(argv)
    tursodb = Path(args.tursodb_exe)
    if not tursodb.is_file():
        print(json.dumps({"verdict": "SKIP", "reason": f"tursodb not found at {tursodb}"}))
        return 2
    raw = REPO_ROOT / "AI" / "work" / "test-integration" / "m17"
    raw.mkdir(parents=True, exist_ok=True)
    plane = raw / "plane"
    if plane.exists():
        shutil.rmtree(plane, ignore_errors=True)
    plane.mkdir(parents=True)
    previous_repo_root = os.environ.get("KAIZEN_REPO_ROOT")
    os.environ["KAIZEN_REPO_ROOT"] = str(plane)
    initialized = subprocess.run(
        [sys.executable, str(REPO_ROOT / "kaizen.py"), "K1", "--json"],
        capture_output=True, cwd=str(REPO_ROOT), env=os.environ, timeout=120,
    )
    if initialized.returncode != 0:
        print(json.dumps({"verdict": "FAIL", "reason": "scratch plane initialization failed"}))
        return 1
    from kaizen_components.fleet import control_http, coordination, dispatch_remote, metrics, reconcile
    from kaizen_components.fleet.store import FleetStore

    for f in raw.glob("*.db*"):
        f.unlink(missing_ok=True)
    port = unused_port()
    server_log = None
    server = None
    verdict: dict = {"steps": {}}
    a = b = None
    service = None
    try:
        server_log = (raw / "server.log").open("wb")
        server = subprocess.Popen(
            [str(tursodb), str(raw / "hub.db"), "--sync-server", f"{HOST}:{port}"],
            stdout=server_log, stderr=subprocess.STDOUT, cwd=str(raw),
        )
        if not wait_port(HOST, port, server):
            raise RuntimeError("sync server never opened")
        time.sleep(0.5)
        sync_url = f"http://{HOST}:{port}"
        a = FleetStore(db_path=raw / "replicaA.db", sync_url=sync_url, node={"node_id": NODE_A}, logger=log)
        b = FleetStore(db_path=raw / "replicaB.db", sync_url=sync_url, node={"node_id": NODE_B}, logger=log)

        # Traffic: registrations + heartbeats; A coordinator; a dispatch A->B accepted/started/completed;
        # a contested claim from B (an OPEN conflict span); then B goes quiet and A sweeps it stale.
        a.register_node("worker", tailnet_name="m17-a.tailtest.ts.net")
        a.heartbeat()
        coordination.claim_coordinator(a, summary="A claims.")
        coordination.grant_lease(a, "kz/m17/work", NODE_A, ttl_s=10**9, summary="A holds.")
        disp = dispatch_remote.request_dispatch(a, target_node=NODE_B, task="m17", scope_key="kz/m17/work", summary="D.")
        dispatch_id = disp["dispatch_id"]
        a.push()
        b.pull_and_reduce()
        b.register_node("worker", tailnet_name="m17-b.tailtest.ts.net")
        b.heartbeat()
        dispatch_remote.accept_dispatch(b, dispatch_id)
        dispatch_remote.start_dispatch(b, dispatch_id)
        time.sleep(1.5)  # Wide margin: requested->completed must remain measurably positive on loaded hosts.
        dispatch_remote.complete_dispatch(b, dispatch_id, artifact_name="patch.diff", artifact_sha="0" * 64)
        coordination.claim_coordinator(b, summary="B contests.")  # roaming contested -> conflict recorded
        coordination.grant_lease(a, "kz/m17/dead", NODE_B, ttl_s=10**9, summary="B will die holding this.")
        b.push()
        a.pull_and_reduce()

        # Orphan-reclaim B (compressed threshold; real elapsed heartbeat age).
        time.sleep(1.5)  # Exceed stale threshold + skew margin without a scheduler-tight assertion.
        swept = reconcile.sweep_stale_nodes(a, stale_after_s=0.5, skew_margin_s=0.2)
        entry = next((s for s in swept["stale"] if s["node"] == NODE_B), None)
        verdict["steps"]["traffic_and_sweep"] = {"swept": entry, "pass": bool(entry and entry["leases_reclaimed"])}

        # THE EXIT: every metric class visible on the survivor's bundle.
        bundle = metrics.fleet_metrics(a)
        checks = {
            "both_heartbeats": len(bundle["heartbeat_ages_s"]) == 2,
            "foreign_staleness_present": bundle["sync_staleness"]["newest_foreign_event_age_s"] is not None,
            "conflict_open": bundle["lease_conflicts"]["open_count"] >= 1,
            "orphan_reclaimed": bundle["orphan_sweeps"]["reclaimed_total"] >= 1,
            "dispatch_latency_measured": (bundle["dispatch_latency"]["terminal_count"] >= 1 and (bundle["dispatch_latency"]["avg_s"] or 0) > 0),
            "sync_stats_healthy": bundle.get("sync_stats", {}).get("sync") == "on",
        }
        verdict["steps"]["metrics_bundle"] = {"bundle": {k: bundle[k] for k in ("heartbeat_ages_s", "sync_staleness", "lease_conflicts", "orphan_sweeps", "dispatch_latency")}, "checks": checks, "pass": all(checks.values())}

        # /v1/health over a REAL socket on the survivor (unsigned read-only; loopback bind = lab form).
        service = control_http.ControlService(
            store=a, identity={"node_id": NODE_A}, bind="127.0.0.1:0", tailnet_probe=lambda: True, logger=log
        )
        service.start()
        host, port = service.address
        with urllib.request.urlopen(f"http://{host}:{port}/v1/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        h_ok = health["status"] == "OK" and health["node_id"] == NODE_A and health["uptime_s"] >= 0 and health["synced"] and health["coord_events"] > 0
        verdict["steps"]["health_endpoint"] = {"health": health, "pass": bool(h_ok)}
    except Exception as error:  # noqa: BLE001 -- verdict must always print
        import traceback

        verdict["error"] = f"{type(error).__name__}: {error}"
        verdict["trace"] = traceback.format_exc()
        log(verdict["trace"])
    finally:
        if service is not None:
            service.stop()
        for store in (a, b):
            if store is not None:
                store.close()
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
        if server_log is not None:
            server_log.close()
        if previous_repo_root is None:
            os.environ.pop("KAIZEN_REPO_ROOT", None)
        else:
            os.environ["KAIZEN_REPO_ROOT"] = previous_repo_root

    steps = verdict["steps"]
    verdict["verdict"] = "PASS" if steps and all(s.get("pass") for s in steps.values()) and "error" not in verdict else "FAIL"
    print(json.dumps(verdict, indent=2, default=str))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
