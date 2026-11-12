# Saved playground states

Save one playground’s resources, including Console edits, consumer applications, subscriptions and API keys. Restore that
playground later without rewinding other playgrounds or the shared databases. Keep several states per playground.

## Save before stopping or switching

1. Open the playground and choose **Stop**, or start another playground that stops it.
2. Choose **Save state and delete what it created**.
3. Confirm. The Lab exports the playground’s resources and consumer access, then runs the usual seeder cleanup.

A failed or unsupported save never starts deletion. If cleanup fails after saving, the completed state stays
available. Saving does not pause the platform or copy a shared database volume.

## Restore a saved state

1. Open the playground's **Saved states** section and choose **Restore** beside an entry.
2. Review which apps will start or stop, and confirm replacing this playground's resources and consumer access.

Restore validates ownership and resource conflicts before switching apps. It starts the playground's apps without
running its seed, then imports only its saved resources and restores its ownership record. Another playground's
resources, including edits made after the save, stay as they are. The usual app switch may stop that playground's
containers; its Gravitee data is kept. Save any current changes to the selected playground that you want to keep.

Use the same Lab installation and management API. The Lab refuses a damaged snapshot, a changed target, or an id,
path or crossId used by an unowned API. Lab settings, connection credentials, image versions, catalogs and shared databases are not
restored. Apply compatible platform versions yourself before restoring.

## Supported resources

The adapters support recorded API Management V4 HTTP proxy APIs, their plans and documentation, plus owned portal
themes, portals, pages and listings. They also save consumer applications and subscriptions to those APIs, including
applications created manually in the Developer Portal. Pending, accepted, paused, closed and rejected subscriptions,
subscription metadata and configuration, issued key values, expiry and revocation are included. Deploy pending API
changes before saving a running API.

An application belongs to this playground while all its subscriptions, including closed ones, point to its APIs and
no other playground records the application. Its settings, owner, members and metadata are restored. A shared
application is a reference: only its subscriptions to this playground’s APIs are restored; its settings, memberships
and subscriptions to other APIs stay as they are. An application that becomes shared after saving, or a key shared
with subscriptions outside this playground, refuses restoration before resources change. A recorded application
that is shared also refuses Save and delete, because the seed’s cleanup would archive it for everyone.

Restore preserves subscription ids and issued key values. Keys issued after the saved state are disabled before the
API starts again. Gravitee may retain those disabled keys in its history, and recreating resources produces new
management audit entries and timestamps. Restore does not rewind analytics or the platform’s audit history.

APIs with shared groups, API Product participation or other API types are refused. Other recorded kinds, including
users, Access Management and Gamma resources, refuse the save before anything is deleted. There is no whole-database
fallback. User accounts and shared groups must still exist; restoring application membership never recreates them.
Older resource snapshots without consumer data preserve current consumers rather than treating them as empty.

Only the selected playground's local ownership record is restored; manually created consumer applications are not
added to the seed’s permanent cleanup record. Unrelated resources, shared environment settings, counters, add-on
storage, application files, container memory and external services are outside the snapshot. Shared theme activation
is kept as it is: restoring an owned theme does not reactivate it for the whole environment. Unrecorded children of
owned portal folders, occupied portal hrids and an unowned active homepage refuse restoration before resources
change. A saved state is not a complete backup of an application. A playground with a recorded Cloud deployment
cannot use these local actions.

## Linked playground folders

Use the same actions in Personal, downloaded catalogs and linked development catalogs. Links and source repositories
stay in place. The Lab binds a state to the selected playground's resolved folder, Compose file, seed files and local
settings. Changing another playground or its source does not block restoration. Changing this playground's inputs or
retargeting its link does: restore the matching inputs yourself before retrying. The Lab never rewrites source files.

## Storage and recovery

The Lab keeps private state directories under `.lab/saved-states/`, outside Git. Each complete state contains a
manifest, a checksum and `resources.json`. API definitions and saved consumer keys contain credentials, so protect these unencrypted
files as you would the live credentials. Never attach them to an issue, pull request or log.

Before writing resources, restore saves only this playground's current resources for rollback. A failed restore
attempts to put those resources back. If recovery fails, keep the reported `.restore-*` directory and its
`previous.json`. Do not reset the Lab or remove those files while diagnosing the failure. An adapter transaction
already running in Docker finishes independently if the web app closes; its `in-flight.json` identifies the container
and `result.json` records completion. Keep its directory until the outcome has been checked.

**Delete saved state** removes only that saved state. **Reset** clears live data and ownership records but keeps saved
state files. Old schema-1 whole-Lab checkpoints are retained on disk, with their image tags, but are hidden from this
list and rejected by restore. They cannot safely be used as playground states.

For the unit, Docker and full Gravitee API checks, see [Change the Lab UI](../CONTRIBUTING.md#change-the-lab-ui).
