# Ticket 01: [Prefactoring] Durable Multi-Worker Storage, Atomic Seed Publication & Unified Vietnam Coordinate Contracts

- **Status**: FREEZE-SPEC v3.0
- **Blocked by**: None
- **Slice Type**: Prefactoring ("Make the change easy, then make the easy change")

## What Can I Demo When This Is Done?
Start the service, create a trip session, shut down the registry, instantiate a fresh worker registry against the same configured database, and verify the session state survives both restarts and cross-worker reads. Run a live filesystem lock probe that uses the primary database path directly when `BEGIN IMMEDIATE` succeeds and falls back to a deterministic shared `/tmp` path (never a random ephemeral directory deleted on exit) when mounted on FUSE/NFS without `mmap`. Send swapped coordinates (`[106.7, 10.7]`) or out-of-bounds latitudes (`23.8`) and receive a deterministic `HTTP 422`.

## Root-Cause Failure Modes Eliminated
1. **Ephemeral Session Loss on Restart / Multi-Worker Isolation**: Copying the database into a random temporary directory that gets deleted on process exit destroys active user sessions on restart and isolates Uvicorn workers from each other.
2. **Non-Atomic Seed Publication**: Overwriting the active SQLite file in place during seeding exposes partial schema/data to concurrent startups.
3. **Contradictory Coordinate Bounds**: Spec drift between `[8.0, 24.0]` and `[8.0, 23.5]` caused inconsistent validation behavior.

## Freeze-Grade Invariants (F1.1 – F1.4)
- **F1.1 (Deterministic Shared Storage & Lock Probe)**: On startup, the storage engine tests the configured database path with a live `BEGIN IMMEDIATE; ROLLBACK;` transaction under `PRAGMA journal_mode = DELETE; PRAGMA synchronous = NORMAL; PRAGMA busy_timeout = 5000;`. If the filesystem supports standard file locking, it uses that exact file in place. Only if `sqlite3.OperationalError` occurs on lock acquisition does it copy/sync to a deterministic shared path (`/tmp/happy_path_shared_< path_hash >.db`) that is **never deleted on shutdown** and is shared across all worker instances.
- **F1.2 (Atomic Seed Publication)**: The database compiler builds tables and indexes inside a staging file in `/tmp` and publishes to the target destination using an atomic OS rename (`os.replace`) after verifying table integrity.
- **F1.3 (Corrupt/Missing Database Fail-Fast)**: If the database file is missing or corrupt (non-SQLite bytes or missing required tables), registry initialization raises a deterministic `RuntimeError` rather than serving empty routes.
- **F1.4 (Unified Vietnam Coordinate Box)**: All coordinate inputs across all documents, schemas, and endpoints enforce `lat in [8.0, 23.5]` and `lng in [102.0, 110.0]`.

## Property-Based Acceptance & Regression Gates
1. **Cross-Worker & Restart Durability Gate**: Create a session in Registry Instance A, close Instance A, instantiate Registry Instance B, and assert the session record, locked stops, rejected stops, and displayed candidate durations load identically.
2. **Corrupt Database Gate**: Pass a truncated/corrupted byte file to the registry initializer and assert it raises `RuntimeError` immediately.
3. **Boundary Coordinate Sweep Gate**: Test `(8.0, 102.0)` [valid], `(23.5, 110.0)` [valid], `(23.6, 106.7)` [HTTP 422], `(10.7, 110.1)` [HTTP 422], and swapped `(106.7, 10.7)` [HTTP 422].
