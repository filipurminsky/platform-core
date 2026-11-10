# Redis replacement and backup recovery

Redis runs as one StatefulSet replica with a PVC, AOF enabled and
`appendfsync everysec`. A replacement can reuse its data, but clients lose service
while that instance is unavailable. The PDB permits one unavailable replica; it
is not a high-availability guarantee. A lost volume needs a separate backup.
Seven-day job and dedup TTLs are expiration settings, not a promise of retention
after storage loss. Power loss can lose recent writes with every-second fsync.

## Repeatable isolated drill

Run `make infrastructure-test` with Docker running. The runner creates a unique
Compose project and two project-owned Redis volumes. It does not touch a
Kubernetes cluster or existing Compose stack. Its EXIT trap deletes both volumes,
containers, network and generated test image, including on failure.

The drill uses the deployed Redis version and persistence settings. After the
four Kafka/worker recovery tests it:

1. Writes synthetic completed and in-progress job documents using the shared
   state helper, plus completed dedup markers and processing leases. `WAITAOF`
   confirms this fixture's writes have reached local AOF storage.
2. Sends SIGKILL and replaces the Redis container, requiring a different
   container ID and the same volume. It verifies exact values and remaining TTLs.
3. Takes a synchronous RDB snapshot (`SAVE`, suitable only for this tiny fixture)
   and copies it out. A subsequent write marks the recovery-point boundary.
4. Starts another Redis instance on a separate empty volume from that snapshot.
   It checks that the saved state exists, TTLs were not reset, and the subsequent
   source write is absent.
5. Enables AOF on the restored instance, waits for its initial rewrite, and
   writes another fsync-confirmed marker. It forcibly replaces this instance too,
   verifying that recovered records **and the post-restore marker** survive from
   AOF, without reloading the original snapshot.

The evidence directory printed by the runner contains `redis-recovery.json`
(measured end-to-end replacement/restore times), per-phase assertion reports,
`redis-backup.rdb` and its SHA-256, worker JUnit results, and infrastructure logs.
Times include container startup, authenticated checks and state assertions; they
are observations on this machine, not production recovery objectives. The
snapshot contains only synthetic test data. Protect real backups as sensitive
job metadata; store them encrypted outside the Redis volume with access controls
and retention appropriate to the application.

The local Docker drill on 2026-11-10 passed all four worker recovery tests
(34.18 seconds), then verified the Redis replacement in **4.550 seconds**, RDB
restore in **5.861 seconds**, and a subsequent replacement using restored AOF in
**4.689 seconds**. All four synthetic job/dedup records and remaining TTLs
matched; the post-backup source write was absent and the post-restore AOF write
survived. The run removed both project volumes afterward. These measurements
cover container/volume recovery only, with the limits below.

## Restore sequence for a real incident

A production restore requires a separately reviewed incident/change procedure;
this drill does not execute one. Preserve the original volume and capture its
persistence files before replacing anything. Stop writers or accept an explicit
backup recovery point, select and verify a known-good backup, and restore into a
**new empty volume** using a compatible Redis version.

Redis must initially load an RDB backup with `appendonly no`. Simply putting
`dump.rdb` in a new directory and starting with `appendonly yes` can start an empty
AOF-backed database instead of loading that snapshot. After confirming loaded
job/dedup values and TTLs, use `CONFIG SET appendonly yes`, wait for a successful
AOF rewrite (`aof_rewrite_in_progress=0`, `aof_last_bgrewrite_status=ok`), and
confirm the next startup configuration has `appendonly yes`. The repository's
normal StatefulSet already starts with AOF enabled; a one-time restore therefore
needs an explicit temporary recovery configuration before returning to it.

Verify the restored instance survives replacement before routing clients to it.
Reconcile Kafka offsets, object keys and restored Redis state: writes after the
snapshot are absent, expired leases can allow redelivery, and restoring dedup
state is not a transaction with Kafka or S3. An old backup can therefore repeat
work. Keep handlers idempotent and examine DLQs before replaying records.

This container drill does not prove Kubernetes PVC reattachment, node failure,
filesystem corruption recovery, backup scheduling/offsite retention, replica
failover, or recovery at production dataset size. Test those separately before
committing to an RPO/RTO. For continuous availability, use a separately designed
Redis HA deployment or managed service.
