"""Signed tamper-evidence VERIFICATION pass (v8 M16, plan §C.4; the read-side counterpart of the M11
sig-on-append seam -- store.py line ~288 explicitly deferred verification here).

M11 attests: every locally-appended coord_event carries ``content_hash`` (``utc_text_hash`` over the
row core) and, when the appender holds a signing seed, ``sig`` (Ed25519 over the content_hash utf-8
bytes, hex). This module VERIFIES pulled/held rows:

- **content**: recompute the hash from the STORED raw row fields (``payload_json`` as stored bytes --
  never re-serialized from a parsed dict, so float/key-order round-trips cannot false-positive) and
  compare to the stored ``content_hash``. Mismatch ⇒ ``tampered_content``.
- **signature**: verify ``sig`` over the RECOMPUTED hash against the signer node's KEY HISTORY --
  the current ``nodes.pubkey`` row plus every pubkey the node ever self-published in a
  ``node/registered``/``node/updated`` payload (the M11 rotation verdict: the ledger itself is the key
  history, so rows signed by rotated-away keys still verify). No resolvable key ⇒ ``unknown_key``;
  no key verifies ⇒ ``invalid_sig``.
- ``sig IS NULL`` ⇒ **unsigned**, counted truthfully, NEVER a failure (pre-M11 rows, seedless lab
  identities, and peers that have not minted keys are all legitimate).

READ-side audit ONLY (M16 decision, RESUME-HERE): verification never gates a write. ``verify_ledger``
optionally APPENDS one advisory ``divergence/detected {would_block: LEDGER_SIG_INVALID, event_id}``
per bad row (record-only, deduped per event id, so a re-verification appends nothing new) -- the same
record-only posture as the M15 stale-coordinator divergence.

**Hash-chain decision (recorded here per the M16 contract):** per-row content_hash+sig SUFFICES for
§C.4's detect/attribute/recover posture at M16 -- attribution is the signature, content tamper is the
hash, hub rewind is the un-synced ``max_seen_epoch`` watermark (DENIED_EPOCH_REGRESSION), and row
DELETION is detectable by cross-replica diff (append-only node-tagged PKs never legitimately vanish).
A per-node hash CHAIN would add ordering/deletion evidence inside a single replica at the cost of
changing the append path fleet-wide mid-plan; DEFERRED owner-gated (see runbooks/security.md).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ..hashing import utc_text_hash

# One advisory divergence per bad event id (dedupe key), mirroring M15's STALE_COORDINATOR posture.
_WOULD_BLOCK = "LEDGER_SIG_INVALID"

_ROW_SQL = (
    "SELECT id, created_at, node_id, project_id, event_kind, marker, scope_key, epoch, "
    "payload_json, content_hash, sig FROM coord_events ORDER BY created_at, id"
)


def key_history(store: Any, node_id: str) -> list[str]:
    """Every pubkey hex ``node_id`` is known by, newest-wins order irrelevant (ANY may verify a row
    signed in its era): the current ``nodes.pubkey`` plus each ``payload.pubkey`` the node itself
    published via a ``node`` kind event (``registered``/``updated`` -- the M11 ``publish_pubkey`` trail).
    Payload keys are accepted only from events APPENDED BY that node (``event.node_id == node_id``):
    a peer cannot inject a key into another node's history."""
    keys: list[str] = []
    reader = getattr(store, "_read_all", None)
    if callable(reader):
        try:
            rows = reader("SELECT pubkey FROM nodes WHERE node_id = ?", (node_id,))
            if rows and rows[0] and rows[0][0]:
                keys.append(str(rows[0][0]))
        except Exception as error:  # noqa: BLE001 -- tolerate only the documented nodes-free lab fake
            detail = str(error).lower()
            if "no such table" not in detail or "nodes" not in detail:
                raise
    for event in store.coord_events():
        if event.get("event_kind") != "node" or event.get("node_id") != node_id:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        pub = payload.get("pubkey")
        if pub and str(pub) not in keys:
            keys.append(str(pub))
    return keys


def _recompute_hash(row: dict[str, Any]) -> str:
    """The EXACT append-time recipe (store.append_coord_event): utc_text_hash over the 8 core fields
    with payload_json as the STORED string."""
    return utc_text_hash(
        {
            "node_id": row["node_id"],
            "project_id": row["project_id"],
            "event_kind": row["event_kind"],
            "marker": row["marker"],
            "scope_key": row["scope_key"],
            "epoch": row["epoch"],
            "payload_json": row["payload_json"],
            "created_at": row["created_at"],
        }
    )


@lru_cache(maxsize=1)
def _nacl_verifier():
    """Load PyNaCl once, lazily, for the signed-row verification path."""
    from nacl import signing
    from nacl.exceptions import BadSignatureError

    return signing, BadSignatureError


def _verify_sig(sig_hex: str, message: str, pubkeys: list[str]) -> bool:
    """Return True when any historical public key verifies sig_hex over UTF-8 message bytes; malformed signatures/keys and bad signatures are skipped and yield False if none verify."""
    signing, bad_signature_error = _nacl_verifier()
    for pub in pubkeys:
        try:
            signing.VerifyKey(bytes.fromhex(pub)).verify(message.encode("utf-8"), bytes.fromhex(sig_hex))
            return True
        except (bad_signature_error, ValueError, TypeError):
            continue
    return False


def _raw_rows(store: Any) -> list[dict[str, Any]]:
    """Return raw stored coord-event columns; hash verification depends on untouched payload JSON."""
    reader = getattr(store, "_read_all", None)
    if not callable(reader):
        raise TypeError("ledger verification requires a store raw-row reader")
    rows = reader(_ROW_SQL)
    cols = ("id", "created_at", "node_id", "project_id", "event_kind", "marker", "scope_key", "epoch", "payload_json", "content_hash", "sig")
    return [dict(zip(cols, row)) for row in rows]


def _existing_divergence_event_ids(store: Any) -> set[str]:
    """Event ids already carrying a LEDGER_SIG_INVALID advisory (the re-verification dedupe set)."""
    flagged: set[str] = set()
    for event in store.coord_events():
        if event.get("event_kind") != "divergence" or event.get("marker") != "detected":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        if payload.get("would_block") == _WOULD_BLOCK and payload.get("event_id"):
            flagged.add(str(payload["event_id"]))
    return flagged


def verify_ledger(store: Any, *, record: bool = False) -> dict[str, Any]:
    """Walk every coord_event raw row and classify it:

    - ``verified`` -- content hash matches AND the signature verifies against the signer's key history;
    - ``unsigned`` -- ``sig IS NULL`` (legitimate: pre-M11 / seedless identities), content hash still
      CHECKED (an unsigned row with a bad hash is ``tampered_content``, not unsigned);
    - ``tampered_content`` -- recomputed hash != stored ``content_hash`` (row bytes were altered);
    - ``invalid_sig`` -- hash intact but NO key in the signer's history verifies the signature;
    - ``unknown_key`` -- signed, but the signer has NO resolvable key at all (cannot verify either way).

    ``record=True`` appends ONE advisory ``divergence/detected {would_block: LEDGER_SIG_INVALID,
    event_id, node, reason}`` per tampered/invalid row (deduped against prior advisories -- a
    re-verification appends nothing). NEVER a write gate; never raises on a bad row.

    Returns ``{status, checked, verified, unsigned, tampered_content: [ids], invalid_sig: [ids],
    unknown_key: [ids], recorded: [advisory ids]}``."""
    rows = _raw_rows(store)
    history_cache: dict[str, list[str]] = {}
    verified = 0
    unsigned = 0
    tampered: list[str] = []
    invalid: list[str] = []
    unknown: list[str] = []

    for row in rows:
        row_id = str(row["id"])
        recomputed = _recompute_hash(row)
        sig = row["sig"]
        if not row["content_hash"]:
            if sig:
                tampered.append(row_id)
            else:
                unsigned += 1
            continue
        if recomputed != str(row["content_hash"]):
            tampered.append(row_id)
            continue
        if not sig:
            unsigned += 1
            continue
        node = str(row["node_id"] or "")
        if node not in history_cache:
            history_cache[node] = key_history(store, node)
        keys = history_cache[node]
        if not keys:
            unknown.append(row_id)
            continue
        if _verify_sig(str(sig), recomputed, keys):
            verified += 1
        else:
            invalid.append(row_id)

    recorded: list[str] = []
    if record and (tampered or invalid):
        already = _existing_divergence_event_ids(store)
        by_id = {str(r["id"]): r for r in rows}
        for reason, ids in (("tampered_content", tampered), ("invalid_sig", invalid)):
            for event_id in ids:
                if event_id in already:
                    continue
                bad = by_id.get(event_id, {})
                appended = store.append_coord_event(
                    "divergence",
                    "detected",
                    summary=f"Ledger row {event_id} failed signature verification ({reason}).",
                    payload={
                        "would_block": _WOULD_BLOCK,
                        "event_id": event_id,
                        "node": bad.get("node_id"),
                        "reason": reason,
                    },
                    source_event_id=f"ledger-verify:{event_id}:{_WOULD_BLOCK}",
                )
                recorded.append(appended["id"])

    return {
        "status": "OK",
        "checked": len(rows),
        "verified": verified,
        "unsigned": unsigned,
        "tampered_content": tampered,
        "invalid_sig": invalid,
        "unknown_key": unknown,
        "recorded": recorded,
    }
