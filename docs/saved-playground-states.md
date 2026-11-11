# Saved playground states

Save a checkpoint of the local Lab before deleting what a playground created. Restore it later to return to its
persistent data, settings and container images. Keep several checkpoints for the same playground.

A checkpoint belongs to a playground, but it captures the whole local Lab. Restoring it replaces other playgrounds'
data too. It does not resume processes, memory, live connections or external services.

## Save before stopping or switching

1. Open the playground and choose **Stop**, or start another playground that stops it.
2. Choose **Save state and delete what it created**.
3. Confirm the action. Follow **Save state**, **Delete what it created**, then the remaining phases of the job.

The Lab stops all running local containers to copy the databases consistently, then restarts those same containers
and waits for them to become healthy. Once the checkpoint is complete, it runs the existing seeder cleanup. Other
local services pause during the save, even if the playground does not use them.

A failed save never starts deletion. If deletion fails after saving, the completed checkpoint stays, and the job
reports the cleanup failure. **Keep what it created** and **Delete what it created** remain available as before.

## Restore a checkpoint

1. Open the playground's **Saved states** section. Each entry shows its name, creation time and archive size.
2. Choose **Restore** beside the entry you want.
3. Confirm replacing the current local Lab's data and configuration.

Save any current changes you want to keep before restoring. The restore includes shared databases and the captured
configuration, including settings, passwords and the license. It starts the services that were running when the state
was saved, using their saved image identities. It does not run the seed again. Changes after a restore can be saved
as another checkpoint by stopping or switching the playground again.

Use the same Lab installation. Restore refuses a changed core `docker-compose.yml`, a damaged archive, a missing
saved image or a saved image tag that points to another image. A checkpoint is not a portable export of a playground:
copying its files alone does not copy the Docker images it needs.

## Supported storage

Apply pending settings before saving, and wait for seeds and other Lab jobs to finish. Saving requires at least one
managed data volume. The Lab supports local Docker volumes with the project's standard names and no driver options.
It refuses anonymous, external and custom-named volumes, and storage managed by another volume driver.

Bind mounts must be inside the captured configuration folders: `.lab/catalogs/`, `.lab/targets/`, `plugins/`,
`secrets/` or `certs/mtls/`, or be one of the captured configuration files. Read-only files elsewhere inside the Lab,
such as its console helper scripts, are checked by content before restore and kept in place. Other external mounts,
unregistered links and special files are refused. An external service or a container's writable layer is not captured.

Saving and restoring are refused while any playground has a recorded deployment in Gravitee Cloud: a local archive
cannot restore those remote resources. Use [Gravitee Cloud's deployment controls](cloud.md#use-both-places) for them.

## Linked playground folders

Use the same Save and Restore actions for a playground in Personal, a downloaded catalog or a linked folder catalog.
No copy or relinking is needed. A registered folder catalog may include links into worktrees inside its source folder.

For a linked folder, the checkpoint saves `.env`, `.state/` and `secrets/` at the catalog root and inside its add-ons and
playgrounds. Restore replaces these local machine files, including the seed's ownership records, while preserving the
catalog link, nested worktree links, source files and Git metadata. The confirmation includes these linked folders.
Writable mounts in linked folders must be inside those machine files.

Keep the source version used for the checkpoint. Restore checks the linked folders' source content and link destinations
before stopping containers or replacing data. If you edit the source, add an item, move a folder or retarget a link,
restore refuses and asks for the saved source version. Revert that source change yourself before restoring; the Lab
never overwrites newer development work to make a checkpoint fit. Git metadata and unrelated worktrees are not restored.

## Files and images

The Lab keeps checkpoints under `.lab/saved-states/`, outside Git. Each contains checksums, compressed volume archives
and a configuration archive. Configuration includes `.env`, `.lab/on.yml`, `.lab/core.json`, `.lab/platforms.json`,
`.lab/targets/`, `.lab/catalogs/`, `.lab/catalogs.json`, `.lab/catalogs.lock`, `plugins/`, `secrets/` and `certs/mtls/` when
present. The catalogs carry the personal files and the seeds' ownership records. Browser preferences and stars are not restored.

Checkpoint directories are private, but the archives are not encrypted. They can contain tokens, passwords,
certificates, a license and application data. Protect copies as you would the live Lab's credentials. Never attach an
archive to an issue or pull request; see [Saved playground states](../SECURITY.md#saved-playground-states).

Docker tags named `gravitee-lab-saved:<state-id>-<number>` keep the saved images available without copying their layers.
Keep those tags and images while you need the checkpoints. Removing them with Docker's image commands can make a
checkpoint unrestorable. Allow space for each checkpoint and for a copy of the current runtime during restore.

## Delete a checkpoint

Choose **Delete saved state** beside an entry and confirm. This removes that checkpoint and releases its image tags;
it changes neither another checkpoint nor the live Lab. Shared image layers stay while another tag or container needs
them. A notice reports image cleanup that Docker could not finish.

**Reset** and `make reset` remove the live containers and volumes and clear the current seeds' records. They do not
remove checkpoint files or their retained images. Delete checkpoints separately when you no longer need them.

## A restore that fails

Before replacing data, the Lab saves the stopped runtime for rollback. If restore or startup fails, it attempts to
put that runtime back. A successful rollback leaves the requested checkpoint unchanged.

If rollback also fails, the job reports **Recovery incomplete** and the path of the retained backup and its manifest under
`.lab/saved-states/.rollback-*/previous/`. Keep that directory and the job output. Do not reset the Lab or remove the
backup while diagnosing the failure. The temporary backup is removed after a successful restore or rollback.

For the unit, Docker and full Gravitee API checks, see [Change the Lab UI](../CONTRIBUTING.md#change-the-lab-ui).
