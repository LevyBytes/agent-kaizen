r"""M9 two-replica convergence lab (the F1 exit criterion): SHIPPED FleetStore x2 over a real local
`tursodb --sync-server`, asserting both replicas reduce to an IDENTICAL fleet digest.

Uses the exact shipped wiring: fleet.store.FleetStore -> fleet.sync.open_connection ->
turso.sync.connect(remote_url=..., bootstrap_if_empty=True) -> push()/pull() -> fresh pure reducers.
Node identities and a lab-scoped project id are INJECTED (no real node_identity.json is minted), and every event is epoch-free, so production project watermarks are neither consulted nor changed.

Run: python tests/integration/m9_lab.py --tursodb-exe <path>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

HOST, PORT = "127.0.0.1", 8091
LAB_PROJECT_ID = hashlib.sha256(b"agent-kaizen-m9-convergence-lab").hexdigest()[:16]


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def wait_port(host: str, port: int, timeout: float = 20.0) -> bool:
    """Return contract: poll TCP connect to host:port until open or timeout; True on first successful connect, False on timeout."""
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def stable_projection(digest: dict) -> dict:
    """The order-stable, node-neutral view both replicas must agree on: node identity facts,
    coordinator, leases, coord-event count. Excludes per-replica fields (self node_id, heartbeat ages,
    sync flag, project source timing)."""
    return {
        "nodes": sorted(
            (n["node_id"], n["role"], n["tailnet_name"], n["last_heartbeat"]) for n in digest["nodes"]
        ),
        "coordinator": digest["coordinator"],
        "leases": digest["leases"],
        "coord_events": digest["counts"]["coord_events"],
        "max_epoch": digest["max_epoch"],
    }


def main(argv: list[str] | None = None) -> int:
    """Entry point: --tursodb-exe arg; return codes 0 PASS / 1 FAIL / 2 SKIP-when-tursodb-missing (load-bearing for callers); always prints a JSON verdict."""
    parser = argparse.ArgumentParser(description="Run the M9 two-replica convergence lab.")
    parser.add_argument("--tursodb-exe", required=True)
    args = parser.parse_args(argv)
    tursodb = Path(args.tursodb_exe)
    if not tursodb.is_file():
        print(json.dumps({"verdict": "SKIP", "reason": f"tursodb not found at {tursodb}"}))
        return 2
    raw = REPO_ROOT / "AI" / "work" / "test-integration" / "m9"
    raw.mkdir(parents=True, exist_ok=True)
    from kaizen_components.fleet import identity
    from kaizen_components.fleet.store import FleetStore

    identity.project_id = lambda: {"project_id": LAB_PROJECT_ID, "source": "m9-lab"}

    hub_db = raw / "hub.db"
    for f in raw.glob("*.db*"):
        f.unlink(missing_ok=True)

    server_log = None
    server = None
    verdict: dict = {"steps": {}}
    a = b = None
    try:
        server_log = (raw / "server.log").open("wb")
        server = subprocess.Popen(
            [str(tursodb), str(hub_db), "--sync-server", f"{HOST}:{PORT}"],
            stdout=server_log, stderr=subprocess.STDOUT, cwd=str(raw),
        )
        if not wait_port(HOST, PORT):
            raise RuntimeError(f"sync server never opened {HOST}:{PORT}")
        time.sleep(0.5)  # TCP accept precedes full sync-server readiness.
        sync_url = f"http://{HOST}:{PORT}"

        # Replica A: register + heartbeat, push-after-commit.
        a = FleetStore(db_path=raw / "replicaA.db", sync_url=sync_url,
                       node={"node_id": "nlab00000000000a"}, logger=log)
        a.register_node("worker", tailnet_name="labnode-a.tailtest.ts.net",
                        capabilities={"has_gpu": True})
        a.heartbeat()
        push_a = a.push()
        verdict["steps"]["A_register_heartbeat_push"] = {"push": push_a, "pass": push_a.get("sync") == "pushed"}

        # Replica B: fresh file bootstraps from the hub; pull -> fresh reducers see node A.
        b = FleetStore(db_path=raw / "replicaB.db", sync_url=sync_url,
                       node={"node_id": "nlab00000000000b"}, logger=log)
        first = b.pull_and_reduce()
        saw_a = any(n["node_id"] == "nlab00000000000a" for n in first["digest"]["nodes"])
        verdict["steps"]["B_bootstrap_sees_A"] = {"nodes": len(first["digest"]["nodes"]), "pass": saw_a}

        # Replica B registers itself, pushes; A pulls; both re-reduce FRESH.
        b.register_node("worker", tailnet_name="labnode-b.tailtest.ts.net")
        push_b = b.push()
        a_after = a.pull_and_reduce()["digest"]
        b_after = b.pull_and_reduce()["digest"]

        proj_a, proj_b = stable_projection(a_after), stable_projection(b_after)
        converged = proj_a == proj_b and len(proj_a["nodes"]) == 2 and proj_a["coord_events"] == 3
        verdict["steps"]["converged_identical_digest"] = {
            "projection_a": proj_a, "projection_b": proj_b,
            "push_b": push_b, "pass": converged,
        }

        # Node-tagged PKs never collide across replicas (LWW merges distinct rows only).
        ids_a = {event["id"] for event in a.coord_events()}
        ids_b = {event["id"] for event in b.coord_events()}
        verdict["steps"]["node_tagged_pks"] = {
            "count": len(ids_a), "identical_sets": ids_a == ids_b,
            "pass": ids_a == ids_b and len(ids_a) == 3,
        }

        # Checkpoint + stats surfaced (loopback-only consumers; revision is per-handle).
        ck = a.checkpoint()
        st = a.stats()
        verdict["steps"]["checkpoint_stats"] = {
            "checkpoint": ck, "stats_keys": sorted(st.keys()),
            "pass": ck.get("sync") == "checkpointed" and st.get("sync") == "on",
        }
    except Exception as error:  # noqa: BLE001 -- verdict must always print
        verdict["error"] = str(error)
    finally:
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

    steps = verdict["steps"]
    verdict["verdict"] = "PASS" if steps and all(s.get("pass") for s in steps.values()) and "error" not in verdict else "FAIL"
    print(json.dumps(verdict, indent=2, default=str))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
