# Saved playground states — implementation design

Status: design and safety contract for `feature/playground-saved-states`. This document does not mean the feature is implemented.

## Goal

A person switching from Playground A to Playground B may choose **Save state and delete what it created**, alongside the unchanged choices **Keep what it created** and **Delete what it created**. The new choice preserves a restorable checkpoint first, then performs the **same seeder cleanup** as Delete. Saved states appear on the existing playground page, where they can be restored or deleted. Each playground may have multiple named, immutable states. Do not add a top-level "Profiles" concept.

## Preserve the existing meaning

- **Keep what it created** stops the playground and retains its live Gravitee resources.
- **Delete what it created** runs the existing seed cleanup before stopping. Its behavior must not change.
- **Save state and delete what it created** creates and verifies a checkpoint **before** invoking that same cleanup. If saving fails, do not run cleanup or switch playgrounds.
- The choice is offered when stopping a playground directly and when starting another one that stops it. Reuse the existing effects dialog and job model.
- Remote Gravitee Cloud resources are **not** captured by a local Docker volume snapshot. Do not offer an "exact" save for remote deployments unless the remote-state problem is explicitly solved; explain why the option is unavailable instead of making a partial backup.

## Exact persistent state: boundaries

"Exact" means restoring managed *persistent* data and configuration, not resuming RAM, processes, in-flight messages or open network connections. Data in external services, host paths, or untracked bind mounts cannot be guaranteed. Before enabling Save, inventory every Compose service and its writable volumes and bind mounts, including those brought by add-ons and the playground. Refuse the exact-save action when a required persistent path cannot be captured; never silently omit it.

The core stores data in Compose volumes such as `gravitee_data-mongo`, `gravitee_data-elasticsearch`, `gravitee_data-redis`, plus additional add-on volumes. These volumes can contain resources belonging to **more than one** playground. A snapshot is therefore of the *local Lab runtime*, associated with the playground that was active, **not** a selective export of that playground's resources. Clearly distinguish it from the seed's `.state/created.json`, which is an ownership record, not a backup.

A checkpoint contains or references:

1. Consistent copies of all relevant named volumes and writable persistent mounts, captured after coordinated shutdown or a storage-specific consistent snapshot.
2. A copy of the resolved Compose configuration, the Lab activation file `.lab/on.yml`, non-secret configuration, secrets (restricted access), installed plugins and required certificates/credential files.
3. The playground's catalog identity and exact revision, personal files if any, seed definitions, and seed ownership records.
4. Exact running container image identifiers/digests, not only version tags; locally built images must remain restorable. This includes databases, APIM, AM, gateways, Gamma and add-ons.
5. A manifest with schema version, playground ID, unique state ID, user-visible name, creation time, completeness, inventory, per-part checksums, size and compatibility diagnostics.

Keep snapshot payloads **outside Git**, for example under an ignored `.lab/saved-states/` directory or a configured private data directory. Protect credentials and private data. An immutable checkpoint is never modified by restoration.

## Safe save transaction

1. Verify that this is a supported local playground and identify every persistent storage dependency, platform image and configuration input. Check available disk space.
2. Prevent other Lab jobs and concurrent start/stop operations.
3. Quiesce/stop writes and capture an internally consistent snapshot into a temporary staging directory. A live `tar` of database volume files is **not** safe.
4. Verify inventory, completeness, checksums and image availability. Atomically publish a complete checkpoint. A partial checkpoint is never listed as restorable.
5. Only now invoke the existing seed deletion step (`_delete_created`) and the existing playground stop plan.
6. If cleanup fails after a valid checkpoint, **retain the checkpoint** and report the failed cleanup. Never report a successful switch when it did not finish.
7. On cancellation, leave no fake complete checkpoint; do not silently delete or corrupt the live runtime.

Snapshotting may require briefly stopping services before the existing deletion step, whose management APIs require running services. The implementation must explicitly restart the required management APIs from the *same unchanged runtime* for cleanup, or use an equivalent safe coordinated snapshot technique. It must not snapshot only after seed deletion.

## Safe restore transaction

1. Offer the playground's saved checkpoints under a **Saved states** section on its existing page. Multiple names/timestamps are supported.
2. Check the candidate's manifest, integrity, catalog revision, image digests, secrets, platform compatibility and remaining storage. Treat a missing image as a blocker, not an invitation to silently pull `latest`.
3. Before any destructive operation, warn that a whole-runtime snapshot would overwrite the current shared core volumes and may erase resources of other playgrounds. Offer to save the current environment first.
4. Prepare restored volumes and configuration in a **separate staging area**; keep the active runtime and checkpoint unchanged until preparation succeeds.
5. Stop the current runtime and switch the prepared data/configuration into place atomically where possible, retaining a rollback copy of the previous runtime until health checks pass.
6. Restore the saved image identities and start the exact compatible stack. Do **not** automatically re-run the seed, because the snapshot already contains the resulting resources.
7. If startup fails, stop the attempted restore and roll back to the previous runtime without modifying the immutable checkpoint.

A restore never downgrades a database or Gravitee component against a newer data directory. It restores the data and image set captured *together*. If any component is unavailable or the platform cannot safely restore, block the operation and say why.

## Data isolation and versioning

Saving A, deleting only A's recorded APIs, and then seeding B on the **same shared MongoDB volume** does not make B a fresh environment. Other persistent resources may remain. Do not promise playground isolation until the active runtime is clean or until an isolated persistent working set is implemented. Prefer one active local working set at a time; checkpoints are immutable and can be restored into a new working copy. The current shared-platform behavior remains the default for existing users.

An initial implementation can support exact snapshots only when the Lab can prove that all relevant state is local and controlled. Add more storage backends incrementally; never weaken the guarantee silently.

## UI

On Stop or Switch, add a third option:

- Keep what it created
- Delete what it created
- Save state and delete what it created

When the third option is selected, request an optional checkpoint name (default to a timestamp). State creation, verification and existing seed deletion belong to **one** job with understandable progress and failure messages. In the playground's existing page show **Saved states** with name, date, source platform version, total size, **Restore** and **Delete**. Deleting a checkpoint requires confirmation and never changes the current live runtime. Support multiple checkpoints, and allow the user to save new changes after restoring one.

## Acceptance tests before release

- Existing Keep and Delete paths remain unchanged (UI and backend tests).
- Switching between two playgrounds with one saved state and restoring it preserves edited APIs and arbitrary data in managed core/add-on volumes.
- Two saved states for one playground are independent; deleting one does not alter the other or live state.
- Missing images, incompatible catalog revision, untracked writable path, insufficient disk, corrupted archive and interrupted save fail safely.
- A failed cleanup leaves a completed snapshot, the actual runtime state is reported accurately, and no new playground is started.
- A failed restore rolls back to the original runtime without corrupting it.
- Any shared-platform data or external dependency that prevents exact isolation blocks the operation or is explicitly called out; never claim isolation from seeder cleanup alone.
- Remote Cloud deployments cannot accidentally trigger a misleading local exact-state snapshot.
- No checkpoint data, secrets, or generated archives are committed to Git.
