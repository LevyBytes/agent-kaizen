r"""M15 offline + recovery lab (the §B.6 exit criteria): two SHIPPED FleetStore replicas over a real
local `tursodb --sync-server`, driven through the full partition lifecycle with REAL wire failures --
the server process is killed and restarted to make partitions, never injected raisers.

Legs (all single-machine per owner decision D7):
  W1 partition:  offline_status detects isolation over the REAL wire; authoritative grant refuses
                 DENIED_LEASE_HUB_UNREACHABLE while the iso path (B.3 isolated fallback: verified
                 contested claim ASSUMES the role, iso-stamped self-grant) keeps local work moving.
  W2 reconnect:  D9 reconcile auto-adopts the iso scope + publishes the iso branch to a scratch bare
                 hub with an ls-remote sha CONFIRM; replicas converge to identical projections;
                 role transferred back to A (serialized B.3 handoff).
  W3 overlap:    second partition; B iso-claims a scope; A (connected) takes the SAME scope
                 authoritatively; B's reconcile surfaces DENIED_ISO_CONFLICT (rank-blind leg: B's iso
                 epoch OUT-RANKS A's grant), records the fork menu, adopts nothing, tree untouched.
  W4 sweep:      false-orphan guard (fresh node untouched at wide threshold), then a compressed-threshold
                 sweep reclaims the stale node's held leases, cancels the open dispatch targeting it,
                 records STALE_COORDINATOR divergence WITHOUT seizing the role; idempotent re-sweep.
  W5 converge:   final push/pull; both replicas reduce to identical stable projections.

Run: python tests/integration/m15_lab.py --tursodb-exe <path>
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
from pathlib import Path
from typing import BinaryIO

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

GIT = shutil.which("git")
HOST = "127.0.0.1"

NODE_A = "nlab0000000m15a"
NODE_B = "nlab0000000m15b"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def unused_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def wait_port(host: str, port: int, proc: subprocess.Popen, timeout: float = 20.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_server(tursodb: Path, raw: Path, hub_db: Path, logf: BinaryIO, port: int) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        [str(tursodb), str(hub_db), "--sync-server", f"{HOST}:{port}"],
        stdout=logf, stderr=subprocess.STDOUT, cwd=str(raw),
    )
    if not wait_port(HOST, port, proc):
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        raise RuntimeError(f"sync server never opened {HOST}:{port} (exit={proc.poll()})")
    time.sleep(0.5)
    if proc.poll() is not None:
        raise RuntimeError(f"sync server exited after opening {HOST}:{port} (exit={proc.returncode})")
    return proc


def stop_server(proc: subprocess.Popen[bytes], port: int) -> None:
    """(optional) asserts the port is CLOSED as the partition precondition AND can raise -> callers in finally must guard (see A1)."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    # The port must actually be CLOSED before the partition legs run.
    end = time.time() + 10
    while time.time() < end:
        try:
            with socket.create_connection((HOST, port), timeout=0.3):
                time.sleep(0.2)
        except OSError:
            return
    raise RuntimeError("sync server port never closed")


def git_run(repo: str):
    def runner(*args: str):
        return subprocess.run([GIT, "-C", repo, *args], capture_output=True, text=True)
    return runner


def stable_projection(digest: dict) -> dict:
    """Order-stable node-neutral view both replicas must agree on (M9-lab convention + M14/M15 keys)."""
    return {
        "nodes": sorted((n["node_id"], n["role"], n["last_heartbeat"]) for n in digest["nodes"]),
        "coordinator": digest["coordinator"],
        "leases": digest["leases"],
        "dispatches": digest.get("dispatches"),
        "conflicts": digest.get("conflicts"),
        "coord_events": digest["counts"]["coord_events"],
        "max_epoch": digest["max_epoch"],
    }


def main(argv: list[str] | None = None) -> int:
    """Orchestration entry; 0=PASS,1=FAIL,2=SKIP exit-code contract (used by CI/callers)."""
    parser = argparse.ArgumentParser(description="Run the M15 offline and recovery lab.")
    parser.add_argument("--tursodb-exe", required=True)
    args = parser.parse_args(argv)
    tursodb = Path(args.tursodb_exe)
    if not tursodb.is_file():
        print(json.dumps({"verdict": "SKIP", "reason": f"tursodb not found at {tursodb}"}))
        return 2
    if GIT is None:
        print(json.dumps({"verdict": "SKIP", "reason": "git not on PATH"}))
        return 2
    raw = REPO_ROOT / "AI" / "work" / "test-integration" / "m15"
    raw.mkdir(parents=True, exist_ok=True)
    plane = raw / "plane"
    if plane.exists():
        shutil.rmtree(plane, ignore_errors=True)
    plane.mkdir(parents=True)
    os.environ["KAIZEN_REPO_ROOT"] = str(plane)
    initialized = subprocess.run(
        [sys.executable, str(REPO_ROOT / "kaizen.py"), "K1", "--json"],
        capture_output=True, cwd=str(REPO_ROOT), env=dict(os.environ), timeout=120,
    )
    if initialized.returncode != 0:
        print(json.dumps({"verdict": "FAIL", "reason": "scratch plane initialization failed"}))
        return 1
    from kaizen_components.denials import KaizenDenied
    from kaizen_components.fleet import coordination, dispatch_remote, reconcile
    from kaizen_components.fleet.store import FleetStore

    for f in raw.glob("*.db*"):
        f.unlink(missing_ok=True)
    hub_db = raw / "hub.db"
    server_log = (raw / "server.log").open("wb")

    # Scratch git plane: B's repo with an iso branch + a bare hub (never the real repo).
    def _rmtree_force(path: Path) -> None:
        # Windows: .git/objects files are read-only -> plain rmtree denies on a warm re-run; chmod+retry.
        def onexc(func, p, _err):  # noqa: ANN001
            os.chmod(p, 0o700)
            func(p)

        shutil.rmtree(path, onexc=onexc)

    repo = raw / "repoB"
    githubdir = raw / "githubbare"
    for d in (repo, githubdir):
        if d.exists():
            _rmtree_force(d)
    subprocess.run([GIT, "init", "-q", "-b", "main", str(repo)], capture_output=True)
    for k, v in (("user.email", "l@l"), ("user.name", "lab"), ("commit.gpgsign", "false")):
        subprocess.run([GIT, "-C", str(repo), "config", k, v], capture_output=True)
    (repo / "work.txt").write_text("base\n", encoding="utf-8")
    subprocess.run([GIT, "-C", str(repo), "add", "-A"], capture_output=True)
    subprocess.run([GIT, "-C", str(repo), "commit", "-qm", "base"], capture_output=True)
    subprocess.run([GIT, "clone", "--bare", "-q", str(repo), str(githubdir)], capture_output=True)

    iso_scope = reconcile.iso_branch("m15", NODE_B)          # adoption leg scope == branch name
    conflict_scope = "kz/m15c/shared"                        # overlap leg scope
    dispatch_scope = "kz/m15d/bait"                          # sweep leg dispatch lease scope

    verdict: dict = {"steps": {}}
    a = b = None
    server = None
    port = unused_port()
    try:
        server = start_server(tursodb, raw, hub_db, server_log, port)
        sync_url = f"http://{HOST}:{port}"
        a = FleetStore(db_path=raw / "replicaA.db", sync_url=sync_url, node={"node_id": NODE_A}, logger=log)
        b = FleetStore(db_path=raw / "replicaB.db", sync_url=sync_url, node={"node_id": NODE_B}, logger=log)

        # --- Setup: A coordinator; both registered + heartbeat; A holds the dispatch-bait lease and
        # dispatches a run to B (sweep-leg bait); converge.
        a.register_node("worker", tailnet_name="m15-a.tailtest.ts.net")
        a.heartbeat()
        coordination.claim_coordinator(a, summary="A claims (roaming).")
        coordination.grant_lease(a, dispatch_scope, NODE_A, ttl_s=10**9, summary="A holds dispatch scope.")
        disp = dispatch_remote.request_dispatch(
            a, target_node=NODE_B, task="m15-bait", scope_key=dispatch_scope, summary="Bait dispatch."
        )
        dispatch_id = disp.get("dispatch_id")
        a.push()
        b.pull_and_reduce()
        b.register_node("worker", tailnet_name="m15-b.tailtest.ts.net")
        b.heartbeat()
        b.push()
        a.pull_and_reduce()
        verdict["steps"]["setup"] = {"dispatch_id": dispatch_id, "pass": True}

        # --- W1 PARTITION: kill the server; isolation must be detected on the REAL wire.
        stop_server(server, port)
        status = reconcile.offline_status(b)
        w1_status_ok = status["isolated"] and status["authoritative_blocked"] and status["can_work_local"]
        verdict["steps"]["W1_real_wire_isolation"] = {"status": status, "pass": bool(w1_status_ok)}

        # B.3 isolated fallback LIVE: contested claim vs A (roaming, unreachable) is VERIFIED then ASSUMED.
        claim = coordination.claim_coordinator(b, isolated=True, summary="B assumes role isolated.")
        w1_claim_ok = claim["status"] == "OK" and claim.get("iso") and claim.get("contested") and claim["holder"] == NODE_B
        verdict["steps"]["W1_iso_fallback_assumes_role"] = {"claim": {k: claim.get(k) for k in ("status", "contested", "iso", "epoch")}, "pass": bool(w1_claim_ok)}

        # Partition rule: NEW cross-host-authoritative mutations pause...
        try:
            coordination.grant_lease(b, iso_scope, NODE_B, mode="authoritative", ttl_s=900, summary="Must refuse.")
            w1_auth = {"raised": False, "pass": False}
        except KaizenDenied as denied:
            w1_auth = {"raised": True, "code": denied.code, "pass": denied.code == "DENIED_LEASE_HUB_UNREACHABLE"}
        verdict["steps"]["W1_authoritative_refused_offline"] = w1_auth

        # ...while LOCAL work continues: iso-stamped advisory self-grant + real git iso branch.
        coordination.grant_lease(b, iso_scope, NODE_B, ttl_s=10**9, summary="Iso self grant.")
        w1_iso_scopes = reconcile._my_iso_scopes(b)
        subprocess.run([GIT, "-C", str(repo), "checkout", "-q", "-b", iso_scope], capture_output=True)
        (repo / "work.txt").write_text("iso work\n", encoding="utf-8")
        subprocess.run([GIT, "-C", str(repo), "commit", "-aqm", "iso work"], capture_output=True)
        subprocess.run([GIT, "-C", str(repo), "checkout", "-q", "main"], capture_output=True)
        verdict["steps"]["W1_local_iso_work"] = {"iso_scopes": w1_iso_scopes, "pass": w1_iso_scopes == [iso_scope]}

        # --- W2 RECONNECT: restart server; reconcile adopts + publishes (owner/lab publish leg).
        server = start_server(tursodb, raw, hub_db, server_log, port)
        rec1 = reconcile.reconcile(b, git_run(str(repo)), hub_remote=str(githubdir), allow_publish=True)
        sha = subprocess.run([GIT, "-C", str(repo), "rev-parse", iso_scope], capture_output=True, text=True).stdout.strip()
        ls = subprocess.run([GIT, "-C", str(repo), "ls-remote", str(githubdir)], capture_output=True, text=True).stdout
        published_confirmed = sha and sha in ls
        w2_ok = rec1["status"] == "OK" and rec1["adopted"] == [iso_scope] and rec1.get("published") == [iso_scope] and published_confirmed
        verdict["steps"]["W2_reconcile_adopts_and_publishes"] = {
            "reconcile": {k: rec1.get(k) for k in ("status", "pulled", "adopted", "published")},
            "hub_sha_confirmed": bool(published_confirmed), "pass": bool(w2_ok),
        }
        b.push()
        a.pull_and_reduce()
        proj_a = stable_projection(a.digest())
        proj_b = stable_projection(b.digest())
        verdict["steps"]["W2_converged"] = {"identical": proj_a == proj_b, "pass": proj_a == proj_b}

        # Serialized role handoff back to A (B is the reduced holder after the iso assumption).
        coordination.transfer_coordinator(b, NODE_A, summary="Role back to A.")
        b.push()
        a.pull_and_reduce()
        coord_after = coordination.current_coordinator(a)
        verdict["steps"]["W2_role_back_to_A"] = {"holder": coord_after.get("holder"), "pass": coord_after.get("holder") == NODE_A}

        # --- W3 OVERLAP RACE: partition again; B iso-claims conflict_scope; A takes it authoritatively.
        stop_server(server, port)
        coordination.claim_coordinator(b, isolated=True, summary="B isolated again.")
        coordination.grant_lease(b, conflict_scope, NODE_B, ttl_s=10**9, summary="Iso grant on contested scope.")
        server = start_server(tursodb, raw, hub_db, server_log, port)
        # A is connected + coordinator in its own replica (B's iso events are unsynced until B pushes).
        grant_a = coordination.grant_lease(a, conflict_scope, NODE_A, mode="authoritative", ttl_s=10**9, summary="A takes the scope.")
        a.push()
        head_before = subprocess.run([GIT, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        try:
            reconcile.reconcile(b, git_run(str(repo)), hub_remote=str(githubdir))
            w3 = {"raised": False, "pass": False}
        except KaizenDenied as denied:
            conflicts = [e for e in b.coord_events() if e["event_kind"] == "conflict" and e["marker"] == "detected" and (e.get("payload") or {}).get("iso")]
            head_after = subprocess.run([GIT, "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
            porcelain = subprocess.run([GIT, "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True).stdout.strip()
            # Idempotency: the W2-adopted scope keeps exactly ONE adoption record (never re-adopted).
            adoption_records = [e for e in b.coord_events() if e["event_kind"] == "resolution" and (e.get("payload") or {}).get("iso_adopted")]
            w3 = {
                "raised": True, "code": denied.code,
                "scopes": denied.fields.get("scopes"),
                "conflict_recorded": len(conflicts) >= 1,
                "tree_untouched": head_before == head_after and porcelain == "",
                "single_adoption_record": len(adoption_records) == 1,
                "pass": (
                    denied.code == "DENIED_ISO_CONFLICT"
                    and denied.fields.get("scopes") == [conflict_scope]
                    and len(conflicts) >= 1
                    and head_before == head_after and porcelain == ""
                    and len(adoption_records) == 1
                ),
            }
        verdict["steps"]["W3_overlap_surfaced_never_merged"] = w3

        # --- W4 SWEEP: B pushes its state so A can SEE it; false-orphan guard first, then reclaim.
        b.push()
        a.pull_and_reduce()
        guard = reconcile.sweep_stale_nodes(a, stale_after_s=3600, skew_margin_s=60)
        guard_ok = not guard["stale"]
        verdict["steps"]["W4_false_orphan_guard"] = {"stale": guard["stale"], "untouched": guard["untouched"], "pass": guard_ok}

        time.sleep(1.2)  # let real elapsed time pass the compressed threshold
        swept = reconcile.sweep_stale_nodes(a, stale_after_s=0.5, skew_margin_s=0.2)
        entry = next((s for s in swept["stale"] if s["node"] == NODE_B), None)
        coord_post = coordination.current_coordinator(a)
        w4_ok = (
            entry is not None
            and conflict_scope in entry["leases_reclaimed"]
            and dispatch_id in entry["dispatches_canceled"]
            and entry["was_coordinator"]
            and coord_post.get("holder") == NODE_B  # divergence recorded, role NEVER auto-seized
        )
        verdict["steps"]["W4_stale_node_reclaimed"] = {"entry": entry, "coordinator_after": coord_post.get("holder"), "pass": bool(w4_ok)}

        before_events = len(a.coord_events())
        second = reconcile.sweep_stale_nodes(a, stale_after_s=0.5, skew_margin_s=0.2)
        after_events = len(a.coord_events())
        entry2 = next((s for s in second["stale"] if s["node"] == NODE_B), None)
        idem_ok = after_events == before_events and entry2 is not None and not entry2["leases_reclaimed"] and not entry2["dispatches_canceled"]
        verdict["steps"]["W4_sweep_idempotent"] = {"events_delta": after_events - before_events, "pass": bool(idem_ok)}

        # --- W5 FINAL CONVERGENCE.
        a.push()
        b.pull_and_reduce()
        proj_a = stable_projection(a.digest())
        proj_b = stable_projection(b.digest())
        verdict["steps"]["W5_final_convergence"] = {"identical": proj_a == proj_b, "projection": proj_a, "pass": proj_a == proj_b}
    except Exception as error:  # noqa: BLE001 -- verdict must always print
        import traceback

        verdict["error"] = f"{type(error).__name__}: {error}"
        verdict["trace"] = traceback.format_exc()
    finally:
        cleanup_errors = []
        for store in (a, b):
            if store is not None:
                try:
                    store.close()
                except Exception as error:  # noqa: BLE001 -- preserve the JSON verdict
                    cleanup_errors.append(f"{type(error).__name__}: {error}")
        if server is not None:
            try:
                stop_server(server, port)
            except Exception as error:  # noqa: BLE001 -- preserve the JSON verdict
                cleanup_errors.append(f"{type(error).__name__}: {error}")
        try:
            server_log.close()
        except Exception as error:  # noqa: BLE001 -- preserve the JSON verdict
            cleanup_errors.append(f"{type(error).__name__}: {error}")
        if cleanup_errors:
            verdict["cleanup_errors"] = cleanup_errors
            verdict.setdefault("error", "cleanup failed: " + "; ".join(cleanup_errors))

    steps = verdict["steps"]
    verdict["verdict"] = "PASS" if steps and all(s.get("pass") for s in steps.values()) and "error" not in verdict else "FAIL"
    print(json.dumps(verdict, indent=2, default=str))
    return 0 if verdict["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
