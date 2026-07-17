"""Fleet distribution plane over a separate, append-only, synchronized coordination database.

- :mod:`coordination` manages leases, coordinators, and publication state.
- :mod:`control_http` serves authenticated fleet control requests.
- :mod:`dispatch_remote` validates and dispatches remote work.
- :mod:`identity` mints and loads node and project identity.
- :mod:`ledger_verify` verifies coordination-ledger integrity.
- :mod:`metrics` derives bounded fleet metrics.
- :mod:`mirror` maintains verified local mirrors.
- :mod:`net` probes tailnet connectivity.
- :mod:`reconcile` reconciles reduced coordination state.
- :mod:`records` implements fleet CLI record handlers.
- :mod:`reducers` deterministically folds coordination events.
- :mod:`store` owns the daemon-held fleet database handle.
- :mod:`sync` wraps synchronized database connections and transaction guards.

``kaizen.db`` never synchronizes; only append-only ``coord_events`` in ``fleet.db`` replicate to the hub. Fleet modules may use subprocesses and sockets by design and are allowlisted by the record-handler import guard.
"""
