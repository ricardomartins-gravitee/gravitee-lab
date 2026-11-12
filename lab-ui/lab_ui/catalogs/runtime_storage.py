"""Inventory and confirmed cleanup of one generated playground runtime.

Only a validated private directory and explicitly owned Docker identities may be removed.
A preview fingerprints the stopped runtime; changed resources require another preview.
Catalog source, shared projects, images and build caches are never cleanup targets here.
"""

import hashlib
import json
import stat

from .. import paths
from ..library import runtime_settings
from ..docker import compose, storage
from ..docker.jobs import JOBS
from . import independent

OPERATIONS = ("containers", "data", "delete")
RUNNING = ("running", "paused", "restarting", "removing")


def known():
    found, problems = [], []
    for file in sorted((paths.ROOT / independent.FOLDER).glob("*/runtime.json")):
        try:
            pid = runtime_settings.alias(json.loads(file.read_text(encoding="utf-8")).get("id"))
            if not paths.is_id(pid) or file.parent != independent.folder(pid):
                raise ValueError("The runtime record does not match its directory")
            found.append((pid, independent.read(pid)))
        except (OSError, ValueError, AttributeError) as error:
            problems.append({"file": str(file.relative_to(paths.ROOT)), "message": str(error)})
    return found, problems


def directory(pid):
    root = paths.ROOT.resolve()
    folder = independent.folder(pid)
    current = root
    for part in folder.relative_to(paths.ROOT).parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("The private runtime directory contains a link; cleanup is held")
    if folder.resolve() != current or not folder.is_dir():
        raise ValueError("The private runtime directory cannot be verified")
    if (folder / "runtime.json").is_symlink() or (folder / "compose.json").is_symlink():
        raise ValueError("The private runtime configuration is a link; cleanup is held")
    return folder


def tree(folder):
    """Allocated bytes and a change fingerprint. Never traverse a link or another filesystem."""
    entries, allocated, files, seen = [], 0, 0, set()
    device = folder.stat().st_dev
    def walk(file):
        nonlocal allocated, files
        item = file.lstat()
        if item.st_dev != device:
            raise ValueError("The runtime contains another filesystem; cleanup is held")
        entries.append((file.relative_to(folder).as_posix(), item.st_ino, item.st_dev, item.st_mode,
                        item.st_size, item.st_mtime_ns, item.st_ctime_ns))
        if stat.S_ISDIR(item.st_mode):
            for child in sorted(file.iterdir()):
                walk(child)
        elif stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode):
            files += 1
            identity = (item.st_dev, item.st_ino)
            if identity not in seen:
                seen.add(identity)
                allocated += getattr(item, "st_blocks", (item.st_size + 511) // 512) * 512
        else:
            raise ValueError("The runtime contains a device, socket or pipe; cleanup is held")
    walk(folder)
    return {"bytes": allocated, "files": files, "fingerprint": _digest(entries)}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _owned(pid, resources):
    project, folder = independent.name(pid), directory(pid)
    for container in resources["containers"]:
        if container["project"] != project or container["directory"] != str(folder):
            raise ValueError("A container does not belong to this runtime directory; cleanup is held")
    ids = {row["id"] for row in resources["containers"]}
    network_users = storage.network_users(resources["networks"])
    for kind in ("volumes", "networks"):
        for row in resources[kind]:
            if row["project"] != project or not row["key"] or row["name"] != project + "_" + row["key"]:
                raise ValueError("A Docker resource has ambiguous ownership; cleanup is held")
            if kind == "networks" and (set(row["containers"]) | network_users[row["id"]]) - ids:
                raise ValueError("A network is used outside this runtime; cleanup is held")
            if kind == "volumes":
                if row["driver"] != "local" or row["options"] or not row["created"]:
                    raise ValueError("A volume is not private local storage; cleanup is held")
                users = set(storage.ask("container", "ls", "-a", "-q", "--no-trunc", "--filter", "volume=" + row["name"]).split())
                if users - ids:
                    raise ValueError("A volume is used outside this runtime; cleanup is held")


def inventory(pid):
    folder = directory(pid)
    value = independent.read(pid)
    if not value:
        raise paths.NotFound("No prepared independent runtime")
    resources = storage.resources(value["project"])
    files, error = None, None
    try:
        _owned(pid, resources)
        files = tree(folder)
    except (OSError, ValueError) as problem:
        error = str(problem)
    return {"resources": resources, "local": files, "error": error,
            "running": any(row["state"] in RUNNING for row in resources["containers"])}


def preview(pid, operation):
    if operation not in OPERATIONS:
        raise ValueError("Choose Remove containers or Delete runtime data")
    registered = runtime_settings.instances().get(pid)
    if operation == "delete" and not registered:
        raise paths.NotFound("No such runtime definition")
    if operation == "delete" and registered and not independent.read(pid):
        if independent.folder(pid).exists() or any(storage.resources(independent.name(pid)).values()):
            raise ValueError("An incomplete runtime has private resources; cleanup is held")
        return {"id": pid, "operation": operation, "fingerprint": _digest({"id": pid, "instance": registered}),
                "removes": {"containers": [], "networks": [], "volumes": [], "folders": [], "runtime": [registered["name"]]},
                "keeps": ["Source playground", "Shared platform", "Other runtimes", "Catalog source and links"], "local_bytes": 0}
    value = inventory(pid)
    if value["error"]:
        raise ValueError(value["error"])
    if value["running"]:
        raise ValueError("Stop this runtime before cleanup")
    resources, files = value["resources"], value["local"]
    fingerprint = _digest({"id": pid, "operation": operation, "resources": resources, "files": files["fingerprint"]})
    return {"id": pid, "operation": operation, "fingerprint": fingerprint,
            "removes": {"containers": [row["name"] for row in resources["containers"]],
                        "networks": [row["name"] for row in resources["networks"]],
                        "volumes": [row["name"] for row in resources["volumes"]] if operation != "containers" else [],
                        "folders": [independent.folder(pid).relative_to(paths.ROOT).as_posix()] if operation != "containers" else []},
            "keeps": ["Source playground", "Shared platform", "Catalog source and links", "Other runtimes", "Images and build cache"]
                     + (["Runtime definition"] if operation == "data" else [])
                     + (["Volumes and runtime files"] if operation == "containers" else []),
            "local_bytes": files["bytes"]}


def _volume_unchanged(row):
    found = storage.inspect("volume", [row["name"]])[0]
    labels = found.get("Labels") or {}
    if (found.get("CreatedAt") != row["created"] or labels.get(storage.PROJECT) != row["project"]
            or labels.get("com.docker.compose.volume") != row["key"] or found.get("Driver") != row["driver"]
            or (found.get("Options") or {}) != row["options"]):
        raise ValueError("A volume changed after confirmation; preview cleanup again")
    if storage.ask("container", "ls", "-a", "-q", "--filter", "volume=" + row["name"]):
        raise ValueError("A volume is now in use; preview cleanup again")


def cleanup(pid, operation, fingerprint):
    if not isinstance(fingerprint, str) or not fingerprint:
        raise ValueError("Preview the exact cleanup before confirming it")
    plan = preview(pid, operation)
    if fingerprint != plan["fingerprint"]:
        raise ValueError("The runtime changed after the preview; review cleanup again")
    def run(job):
        if operation == "delete" and not independent.read(pid):
            if preview(pid, operation)["fingerprint"] != fingerprint:
                raise ValueError("The runtime changed after confirmation; preview cleanup again")
            job.step("Delete the empty runtime")
            runtime_settings.forget(pid)
            return
        if operation != "containers":
            job.step("Prepare file cleanup")
            rc, _, _ = compose.run(["docker", "image", "inspect", storage.FILES_IMAGE], timeout=15)
            if rc:
                job.exec(["docker", "pull", storage.FILES_IMAGE])
        # Recheck after a queued job or image download, before removing the first object.
        if preview(pid, operation)["fingerprint"] != fingerprint:
            raise ValueError("The runtime changed after confirmation; preview cleanup again")
        value = inventory(pid)
        resources = value["resources"]
        job.step("Remove runtime containers and networks")
        for kind, key in (("container", "containers"), ("network", "networks")):
            ids = [row["id"] for row in resources[key]]
            if kind == "network":
                expected = {c["id"] for c in resources["containers"]}
                if any(users - expected for users in storage.network_users(resources[key]).values()):
                    raise ValueError("A network is now used outside this runtime; preview cleanup again")
            if ids:
                job.exec(["docker", kind, "rm", *ids])
        if operation != "containers":
            job.step("Delete private volumes and runtime files")
            for row in resources["volumes"]:
                _volume_unchanged(row)
                job.exec(["docker", "volume", "rm", row["name"]])
            folder = directory(pid)
            if tree(folder)["fingerprint"] != value["local"]["fingerprint"]:
                raise ValueError("Runtime files changed; preview cleanup again")
            manifest = hashlib.sha256((folder / "runtime.json").read_bytes()).hexdigest()
            job.exec(storage.remove_files_command(folder, manifest))
            directory(pid).rmdir()
            if operation == "delete":
                runtime_settings.forget(pid)
        else:
            record = independent.read(pid)
            record["active"] = False
            independent._write(independent.folder(pid) / "runtime.json", record)
    return JOBS.start("Remove runtime containers" if operation == "containers" else "Delete runtime" if operation == "delete" else "Delete runtime data", [run],
                      key=f"playground:{pid}", verb="stop")
