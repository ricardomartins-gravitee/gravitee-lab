"""Local, immutable saved states of a playground's managed persistent runtime.

These are checkpoints of the whole local Lab, not selective API exports. The database volumes can
also hold other playgrounds' data. Only local Docker-managed volumes and files under the Lab root
can be captured; unsupported bind mounts refuse a save.
"""

import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from ..docker import compose
from ..docker.jobs import JOBS, JobFailed
from ..gravitee import places
from . import saved_catalogs

ROOT = paths.ROOT
STATES = ROOT / ".lab" / "saved-states"
IMAGE = "alpine:3.20"
SID = re.compile(r"[a-f0-9]{32}")
MAX_MANIFEST = 1 << 20
# The files that determine a Lab's running state. Each playground's and add-on's settings,
# seed ownership record and catalog revision lives under .lab/catalogs.
CONFIG = (".env", ".lab/on.yml", ".lab/core.json", ".lab/platforms.json", ".lab/targets", ".lab/catalogs",
          ".lab/catalogs.json", ".lab/catalogs.lock",
          "plugins", "secrets", "certs/mtls")
EXCLUDE = (".lab/saved-states", ".lab/lab-ui.flock", ".git")
SCHEMA = 1


def _home(pid):
    # A full-length digest also avoids a slash, @, or a future naming change in a path.
    return STATES / hashlib.sha256(pid.encode("utf-8")).hexdigest()


def _location(pid, sid):
    if not isinstance(sid, str) or not SID.fullmatch(sid):
        raise ValueError("Invalid saved state")
    home = _home(pid)
    target = home / sid
    if any(path.is_symlink() for path in (ROOT / ".lab", STATES, home, target)):
        raise ValueError("Saved state storage must not be linked")
    return target


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST:
        raise ValueError("The saved state manifest is not a regular file")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(obj, dict) or type(obj.get("schema")) is not int
            or obj["schema"] != SCHEMA or obj.get("complete") is not True):
        raise ValueError("Incomplete or unsupported saved state")
    if (not all(isinstance(obj.get(key), str) and obj[key] for key in ("id", "playground", "name", "created"))
            or type(obj.get("size")) is not int or obj["size"] < 0
            or not isinstance(obj.get("images"), list) or not all(isinstance(item, dict) for item in obj["images"])):
        raise ValueError("Invalid saved state metadata")
    try:
        datetime.fromisoformat(obj["created"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid saved state creation time") from None
    return obj


def list_states(pid):
    """Only complete, regular manifests are exposed; partial saves are never restorable."""
    home = _home(pid)
    if (ROOT / ".lab").is_symlink() or STATES.is_symlink() or not home.is_dir() or home.is_symlink():
        return []
    found = []
    for path in home.iterdir():
        if path.is_symlink() or not path.is_dir() or not SID.fullmatch(path.name):
            continue
        try:
            meta = _read(path / "manifest.json")
            if meta.get("playground") != pid or meta.get("id") != path.name:
                continue
            found.append({key: meta[key] for key in ("id", "name", "created", "size", "images")
                          if key in meta})
        except (ValueError, OSError, KeyError, RecursionError):
            continue
    return sorted(found, key=lambda row: row["created"], reverse=True)


def delete_state(project, pid, sid):
    """Delete checkpoint files only. Never change the active Lab or its volumes."""
    if pid not in project["playgrounds"]:
        raise paths.NotFound(f"No such playground: {pid}")
    if STATES.is_symlink():
        raise ValueError("Saved state storage is linked outside the Lab")
    target = _location(pid, sid)
    meta = _read(target / "manifest.json")
    if meta.get("playground") != pid or meta.get("id") != sid:
        raise ValueError("The saved state does not belong to this playground")
    with JOBS.idle():
        shutil.rmtree(target)
    tags = {image.get("locked_tag") for image in meta.get("images") or [] if image.get("locked_tag")}
    return {"deleted": sid, "image_cleanup_pending": bool(_release_images(tags))}


def _docker(args):
    rc, stdout, stderr = compose.run(["docker", *args], timeout=45)
    if rc:
        raise JobFailed((stderr or stdout).strip() or "Docker did not answer")
    return stdout.strip()


def _check_applied(containers):
    """Do not save a new .env/config with old running image state ('Apply needed').

    The checkpoint must describe the settings *used* by the containers it saved. A
    platform upgrade prepared by the next playground is therefore never mistaken for
    the version that created the old playground's database.
    """
    if not containers:
        return
    files = ["-f", str(ROOT / "docker-compose.yml")]
    if (ROOT / ".lab/on.yml").is_file():
        files += ["-f", str(ROOT / ".lab/on.yml")]
    hashes = compose.config_hashes(files)
    if hashes is None:
        raise JobFailed("Could not check applied Compose configuration")
    for container in containers:
        if not container.get("State", {}).get("Running"):
            continue
        labels = (container.get("Config") or {}).get("Labels") or {}
        name = labels.get("com.docker.compose.service")
        if not name or hashes.get(name) != labels.get("com.docker.compose.config-hash"):
            raise JobFailed("Apply the pending settings before saving the playground's state: " + str(name))


def _runtime():
    """Read active and inactive Compose containers and all managed volume names."""
    name = compose.project_name()
    ids = _docker(["ps", "-aq", "--filter", f"label=com.docker.compose.project={name}"]).split()
    containers = json.loads(_docker(["inspect", *ids])) if ids else []
    _check_applied(containers)
    volumes = _docker(["volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={name}"]).split()
    if any(not name_of.startswith(name + "_") for name_of in volumes):
        raise JobFailed("Custom volume names need a storage adapter for exact saved states")
    if volumes:
        details = json.loads(_docker(["volume", "inspect", *volumes]))
        if any(v.get("Driver") != "local" or v.get("Options") for v in details):
            raise JobFailed("Only local Docker volumes without driver options can be saved")
    mounts = []
    running = []
    images = []
    for container in containers:
        cfg = container.get("Config") or {}
        labels = cfg.get("Labels") or {}
        if container.get("State", {}).get("Running"):
            if labels.get("io.gravitee.role") == "seed" or labels.get("com.docker.compose.oneoff") == "True":
                raise JobFailed("Wait for the running seed or one-off container before saving")
            running.append(container["Id"])
            # Restore starts only this running set. An exited seed's obsolete image is not a prerequisite.
            images.append({"service": labels.get("com.docker.compose.service"), "reference": cfg.get("Image"),
                           "id": container.get("Image"), "running": True, "role": labels.get("io.gravitee.role")})
        for mount in container.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name") not in volumes:
                raise JobFailed("The Lab cannot safely snapshot an external or untracked Docker volume: "
                                + str(mount.get("Name")))
            elif mount.get("Type") == "bind":
                mounts.append(mount)
            elif mount.get("Type") not in ("tmpfs", "volume"):
                raise JobFailed(f"Unsupported storage mount: {mount.get('Type')}")
    return sorted(volumes), mounts, running, images


def _inside_root(path):
    base = ROOT.resolve()
    resolved = saved_catalogs.source_path(path)
    try:
        rel = resolved.relative_to(base)
    except ValueError:
        return None
    if any(part in ("..", ".git") for part in rel.parts):
        return None
    return str(rel)


def _configured(relative):
    return relative is not None and any(relative == item or relative.startswith(item + "/") for item in CONFIG)


def _paths_to_save(mounts):
    catalogs = saved_catalogs.read(ROOT)
    for mount in mounts:
        if saved_catalogs.accepts(ROOT, catalogs, mount):
            continue
        relative = _inside_root(mount.get("Source") or "/")
        if relative is None or relative.startswith(".lab/saved-states"):
            raise JobFailed("An external bind mount cannot be saved exactly: " + mount["Destination"])
        if not _configured(relative):
            if mount.get("RW") is False and (ROOT / relative).is_file():
                continue
            raise JobFailed("This bind mount needs a storage adapter before its state can be saved: "
                            + mount["Destination"])
    return sorted(CONFIG)


def _include(info):
    relative = info.name.removeprefix("./")
    if any(relative == prefix or relative.startswith(prefix + "/") for prefix in EXCLUDE):
        return None
    # Refuse symlinks: a malicious catalog must not redirect an archive outside the Lab.
    if info.issym() or info.islnk() or not (info.isdir() or info.isfile()):
        raise JobFailed("Unsupported file in saved configuration: " + relative)
    return info


def _archive_config(destination, mounts):
    catalogs = saved_catalogs.read(ROOT)
    linked = {".lab/catalogs/" + item["id"] for item in catalogs}
    readonly = set()
    for mount in mounts:
        relative = _inside_root(mount.get("Source") or "/")
        if (mount.get("RW") is False and relative is not None and not _configured(relative)
                and not saved_catalogs.accepts(ROOT, catalogs, mount)):
            readonly.add(relative)

    def include(info):
        return None if info.name in linked else _include(info)

    with tarfile.open(destination, "w:gz") as archive:
        for name in _paths_to_save(mounts):
            path = ROOT / name
            if path.exists():
                archive.add(path, arcname=name, recursive=True, filter=include)
        saved_catalogs.add(archive, ROOT, catalogs, sorted(readonly))


def _volume(job, name, archive, restore=False):
    """Tar operates in a disposable container. The database writers have been stopped."""
    if restore:
        cmd = ("find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} \\; && "
               f"tar -xzf /backup/{archive.name} -C /data")
        job.exec(["docker", "run", "--rm", "--pull=missing", "-v", f"{name}:/data",
                  "-v", f"{archive.parent}:/backup:ro", IMAGE, "sh", "-c", cmd])
    else:
        job.exec(["docker", "run", "--rm", "--pull=missing", "-v", f"{name}:/data:ro",
                  "-v", f"{archive.parent}:/backup", IMAGE, "tar", "-czf",
                  f"/backup/{archive.name}", "-C", "/data", "."])


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for data in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def _capture_stopped(job, folder, pid, label, volumes, mounts, running, images):
    """Called with the whole local Lab stopped. Manifest is published last."""
    folder.mkdir(parents=True, exist_ok=False, mode=0o700)
    _archive_config(folder / "config.tar.gz", mounts)
    entries = []
    for index, name in enumerate(volumes):
        archive = folder / f"volume-{index}.tar.gz"
        _volume(job, name, archive)
        entries.append({"name": name, "file": archive.name, "sha256": _digest(archive)})
    files = [{"file": "config.tar.gz", "sha256": _digest(folder / "config.tar.gz")}, *entries]
    created = datetime.now(timezone.utc)
    name = label or f"Saved {created:%Y-%m-%d %H:%M:%S UTC} ({folder.name[-6:]})"
    return {"schema": SCHEMA, "complete": True, "playground": pid,
            "id": folder.name, "name": name, "created": created.isoformat(), "volumes": entries,
            "config": files[0], "images": images, "running": running,
            "size": sum((folder / record["file"]).stat().st_size for record in files),
            "core_sha256": _digest(ROOT / "docker-compose.yml")}


def _pin_images(job, images, sid):
    """Keep exact image IDs from pruning without duplicating their Docker layers."""
    locked, seen = [], {}
    try:
        for item in images:
            if item.get("running") is False:
                continue
            iid = item.get("id")
            if not iid:
                continue
            if iid not in seen:
                tag = f"gravitee-lab-saved:{sid}-{len(seen)}"
                job.exec(["docker", "tag", iid, tag])
                seen[iid] = tag
                locked.append(tag)
            item["locked_tag"] = seen[iid]
    except BaseException:
        _release_images(locked, job)
        raise
    return locked


def _write_manifest(folder, meta):
    # The complete marker only appears after all volume archives exist.
    paths.write_atomic(folder / "manifest.json", json.dumps(meta, indent=2) + "\n", mode=0o600)


def _stop(job, names):
    if names:
        job.exec(["docker", "stop", *names])


def _restart(job, names):
    if names:
        job.exec(["docker", "start", *names])
        # Health-checking services may take several minutes, especially the management APIs.
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            result = json.loads(_docker(["inspect", *names]))
            if all(c.get("State", {}).get("Running") and
                   (not c.get("State", {}).get("Health") or
                    c["State"]["Health"]["Status"] == "healthy") for c in result):
                return
            time.sleep(2)
        raise JobFailed("The platform did not become healthy after taking the checkpoint")


def capture(job, project, pid, name=None):
    """Save an immutable checkpoint before the existing seeder cleanup.

    Stops the whole local Lab momentarily to keep databases consistent; starts exactly
    the containers that ran before, so the existing deletion step can use the APIs.
    """
    if pid not in project["playgrounds"]:
        raise paths.NotFound(f"No such playground: {pid}")
    if any(places.recorded(uid) for uid in project["playgrounds"]):
        raise JobFailed("Local saved states cannot capture the Lab while any playground holds resources in Gravitee Cloud")
    job.step("Save state")
    volumes, mounts, running, images = _runtime()
    if not volumes:
        raise JobFailed("No managed data volumes were found for this Lab")
    _paths_to_save(mounts)  # Validate external mounts before stopping anything
    home = _home(pid)
    if (ROOT / ".lab").is_symlink() or STATES.is_symlink() or home.is_symlink():
        raise JobFailed("Saved states folder must not be a symlink")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    sid = uuid.uuid4().hex
    staged = home / (".pending-" + sid)
    final = home / sid
    finished = False
    stopped = False
    locked = []
    try:
        # Retain and verify required images before any interruption of the live services.
        locked = _pin_images(job, images, sid)
        stopped = True
        _stop(job, running)
        data = _capture_stopped(job, staged, pid, name, volumes, mounts, running, images)
        data["id"] = sid
        _write_manifest(staged, data)
        _restart(job, running)
        stopped = False
        staged.rename(final)
        finished = True
        job.lines.append("Saved checkpoint: " + data["name"])
    finally:
        try:
            if stopped:
                # Recovery must run even if the job was canceled while stopping containers.
                _restart(_Recovery(job), running)
        finally:
            if not finished:
                _discard(job, staged, locked)
    return sid


def _release_images(tags, job=None):
    """A cleanup failure is reported without hiding the original save or deletion result."""
    failed = []
    for tag in tags:
        try:
            _docker(["image", "rm", tag])
        except JobFailed as error:
            failed.append(tag)
            if job is not None:
                job.lines.append(f"Could not release saved image {tag}: {error}")
    return failed


def _discard(job, staged, locked):
    _release_images(locked, job)
    if staged.exists():
        shutil.rmtree(staged)


class _Recovery:
    """Cancellation ends the requested operation, but must not interrupt recovery."""

    def __init__(self, job):
        self.lines = job.lines

    def exec(self, cmd):
        rc, out, err = compose.run(cmd, timeout=360)
        if rc:
            raise JobFailed((err or out).strip() or "Runtime recovery failed")


def _verified(pid, sid):
    if STATES.is_symlink():
        raise ValueError("Saved state storage is linked outside the Lab")
    folder = _location(pid, sid)
    data = _read(folder / "manifest.json")
    if data.get("id") != sid or data.get("playground") != pid:
        raise ValueError("The saved state belongs to another playground")
    if data.get("core_sha256") != _digest(ROOT / "docker-compose.yml"):
        raise RuntimeError("The Lab's core Compose file changed since this state was saved")
    project = compose.project_name()
    for volume in data.get("volumes") or []:
        name = volume.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name) or not name.startswith(project + "_"):
            raise ValueError("The saved state contains a volume outside this Lab")
    existing = set(_docker(["volume", "ls", "-q"]).split())
    for volume in data.get("volumes") or []:
        if volume["name"] in existing:
            info = json.loads(_docker(["volume", "inspect", volume["name"]]))[0]
            if (info.get("Labels") or {}).get("com.docker.compose.project") != project:
                raise ValueError("A saved volume name is now used outside this Lab")
    for entry in [data["config"], *(data.get("volumes") or [])]:
        name = entry.get("file") or ""
        file = folder / name
        if not re.fullmatch(r"(config|volume-\d+)\.tar\.gz", name) or file.is_symlink() or not file.is_file():
            raise ValueError("A saved state archive is missing")
        if _digest(file) != entry.get("sha256"):
            raise ValueError("Saved state archive integrity check failed")
    _config_members(folder)
    for item in data.get("images") or []:
        if item.get("running") is False:
            continue
        if item.get("id"):
            _docker(["image", "inspect", item["id"]])
        if item.get("locked_tag"):
            reference = _docker(["image", "inspect", "--format", "{{.Id}}", item["locked_tag"]])
            if reference != item.get("id"):
                raise ValueError("A saved image tag no longer points to its original image")
    return folder, data


def _config_members(folder):
    """Validate the archive and its destination before any live data is replaced."""
    with tarfile.open(folder / "config.tar.gz", "r:gz") as archive:
        members = archive.getmembers()
        catalogs = saved_catalogs.verified(archive, ROOT)
        linked = {".lab/catalogs/" + item["id"] for item in catalogs}
        seen = set()
        for item in members:
            name = item.name
            if name in seen or name.startswith("/") or ".." in Path(name).parts or _include(item) is None:
                raise ValueError("Unsafe saved configuration path")
            seen.add(name)
            if name in (saved_catalogs.RECORD, saved_catalogs.READONLY_RECORD) or saved_catalogs.member(name, catalogs):
                continue
            if any(name == path or name.startswith(path + "/") for path in linked):
                raise ValueError("Linked catalog source must not be replaced by a configuration archive")
            if not any(name == root or name.startswith(root + "/") for root in CONFIG):
                raise ValueError("Saved state includes a path outside supported configuration")
        for name in CONFIG:
            target = ROOT / name
            if any(part.is_symlink() for part in (target, *target.parents) if part == ROOT or ROOT in part.parents):
                raise ValueError("Cannot restore over a linked configuration path")
        return members


def _restore_config(folder):
    """Replace managed config and secret files, never the saved-state store or Git."""
    members = _config_members(folder)
    with tarfile.open(folder / "config.tar.gz", "r:gz") as archive:
        catalogs = saved_catalogs.verified(archive, ROOT)
        linked = {item["id"] for item in catalogs}
        # Remove stale files that were not present when the snapshot was taken.
        # The backup stays outside these paths and is used on any rollback.
        for name in CONFIG:
            target = ROOT / name
            if target.is_symlink():
                raise ValueError("Cannot restore over a linked configuration path")
            if name == ".lab/catalogs" and linked:
                for child in target.iterdir():
                    if child.name in linked:
                        continue
                    if child.is_symlink() or not child.is_dir():
                        child.unlink()
                    else:
                        shutil.rmtree(child)
                continue
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        directories = []
        for item in members:
            if item.name in (saved_catalogs.RECORD, saved_catalogs.READONLY_RECORD) or saved_catalogs.member(item.name, catalogs):
                continue
            target = ROOT / item.name
            if item.isdir():
                target.mkdir(parents=True, exist_ok=True)
                directories.append((target, item.mode & 0o777))
            elif item.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(item) as source, open(target, "wb") as dest:
                    shutil.copyfileobj(source, dest)
                os.chmod(target, item.mode & 0o777)
        # Set directory modes last: private or read-only folders must not block extraction of their children.
        for target, mode in reversed(directories):
            os.chmod(target, mode)
        saved_catalogs.restore(archive, ROOT, catalogs)


def _retag_images(job, images):
    for item in images:
        if item.get("running") is False:
            continue
        reference, iid = item.get("reference"), item.get("id")
        if reference and iid and not reference.startswith("sha256:") and "@" not in reference:
            job.exec(["docker", "tag", iid, reference])


def restore(project, pid, sid, confirmed=False):
    """Restore the entire local runtime; never partially replace a live Lab without rollback."""
    if pid not in project["playgrounds"]:
        raise paths.NotFound(f"No such playground: {pid}")
    if not confirmed:
        raise ValueError("Confirm replacing the current local Lab data before restoring")
    if any(places.recorded(uid) for uid in project["playgrounds"]):
        raise ValueError("Cannot replace local configuration while playgrounds hold resources in Gravitee Cloud")
    folder, data = _verified(pid, sid)
    # Verify storage inputs and reject foreign writable bind mounts before stopping the Lab.
    _paths_to_save(_runtime()[1])

    def action(job):
        job.step("Restore state")
        # Recheck after the job owns the runtime: no stale validation or deleted checkpoint.
        folder, data = _verified(pid, sid)
        volumes, mounts, running, images = _runtime()
        _paths_to_save(mounts)
        rollback = Path(tempfile.mkdtemp(prefix=".rollback-", dir=str(STATES)))
        current = None
        recovered = False
        try:
            _stop(job, running)
            # Capture the current runtime while it is quiescent for rollback.
            current = _capture_stopped(job, rollback / "previous", pid, "Rollback",
                                       volumes, mounts, running, images)
            _write_manifest(rollback / "previous", current)
            _apply(job, data, folder, volumes)
            recovered = True
        except BaseException:
            job.lines.append("Restore failed; restoring the previous local runtime")
            recovery = _Recovery(job)
            try:
                if current is None:
                    _restart(recovery, running)
                else:
                    _apply(recovery, current, rollback / "previous",
                           set(volumes) | {item["name"] for item in data["volumes"]})
                recovered = True
            except BaseException:
                job.lines.append("Recovery incomplete; previous data retained at " + str(rollback))
                raise
            raise
        finally:
            if recovered:
                shutil.rmtree(rollback)
    return JOBS.start("Restore " + data["name"], [action], key=f"playground:{pid}",
                      verb="restore", phases=["Restore state"], waits=[None])


def _apply(job, data, folder, volumes):
    """Replace a stopped runtime, for a restore or its rollback, without executing seeds."""
    job.exec([*compose.by_name(), "down"])
    desired = {item["name"] for item in data["volumes"]}
    for name in sorted(set(volumes) - desired):
        # A failed apply may not have created all of its volumes yet.
        if name in _docker(["volume", "ls", "-q"]).split():
            _docker(["volume", "rm", name])
    project = compose.project_name()
    for item in data["volumes"]:
        name = item["name"]
        _docker(["volume", "create", "--label", "com.docker.compose.project=" + project,
                 "--label", "com.docker.compose.volume=" + name[len(project) + 1:], name])
        _volume(job, name, folder / item["file"], restore=True)
    _restore_config(folder)
    _retag_images(job, data["images"])
    services = [x["service"] for x in data["images"] if x.get("running") and x.get("service")
                and x.get("role") != "seed"]
    if services:
        job.exec([*compose.COMPOSE, "up", "-d", "--no-deps", "--no-build", "--pull", "never", *services])
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            now = compose.ps()
            if all(svc in now and now[svc].get("state") == "running" and
                   now[svc].get("health") in ("", "healthy") for svc in services):
                return
            time.sleep(2)
        raise JobFailed("Restored services did not become healthy")
