"""Create stopped child runtimes without writing to their source or catalog.

Both Shared and private projects copy their complete verified storage while their
writers are briefly paused. The resolved configuration and exact container images
travel with the snapshot; no Gravitee resources are exported or rewritten. Incomplete
targets stay stopped and marked unusable, with their definition retained for cleanup.
"""

import copy
import hashlib
import json
import os
import time
from pathlib import Path

from .. import paths
from ..docker import compose, storage
from ..docker.jobs import JOBS, JobFailed
from ..library import runtime_settings
from . import independent, runtime_storage, standalone


def _configuration(pid):
    if runtime_settings.mode(pid) == "independent":
        value = independent.read(pid)
        if not value or not value.get("initialized") or value.get("clone_pending"):
            raise ValueError("Start this runtime successfully before cloning it")
        return json.loads((runtime_storage.directory(pid) / "compose.json").read_text(encoding="utf-8"))
    gathered = standalone._gather(pid)
    config = compose.config_json(["-f", paths.CORE_FILE, "-f", paths.ON_FILE], timeout=60)
    own = {key for key, service in gathered["found"]["services"].items()
           if service.get("owner", {}).get("kind") == "playground" and service["owner"].get("id") == pid}
    if not own <= set(config["services"]):
        raise ValueError("Start the source playground successfully before cloning Shared")
    rows = storage.resources(config["name"])
    services = {row["service"] for row in rows["containers"]}
    if services - set(config["services"]):
        raise ValueError("The Shared runtime has containers outside its current configuration")
    required = standalone._keep(config, gathered, standalone._products(gathered))
    lasting = {key for key in required if config["services"][key].get("restart") != "no"
               and config["services"][key].get("labels", {}).get("io.gravitee.role") != "seed"}
    if not lasting <= services:
        raise ValueError("Start the source playground successfully before cloning Shared")
    config["services"] = {key: value for key, value in config["services"].items() if key in services}
    return config


def _running(pid, config):
    rows = storage.resources(config["name"])
    lasting = {key for key, service in config["services"].items()
               if service.get("restart") != "no" and service.get("labels", {}).get("io.gravitee.role") != "seed"}
    live = {row["service"] for row in rows["containers"] if row["state"] == "running"}
    if not lasting or not lasting <= live:
        raise ValueError("Start the source runtime successfully before cloning it")
    if any(row["state"] not in ("running", "exited", "created") for row in rows["containers"]):
        raise ValueError("Wait until the source containers are stable before cloning")
    if runtime_settings.mode(pid) == "independent":
        runtime_storage._owned(pid, rows)
    elif any(row["project"] != config["name"] or not row["directory"]
             or Path(row["directory"]).resolve() != paths.ROOT.resolve() for row in rows["containers"]):
        raise ValueError("A Shared container belongs to another Lab directory")
    return rows


def check(pid):
    config = _configuration(pid)
    _running(pid, config)
    return config


def reason(pid):
    try:
        check(pid)
        return ""
    except (OSError, ValueError, compose.ComposeError) as error:
        return str(error)


def _execute(command, timeout=600):
    rc, out, err = compose.run(command, timeout=timeout)
    if rc:
        # Configuration, API responses and archived files can contain secrets.
        raise JobFailed("Could not copy the runtime; its source was preserved")
    return out.strip()


def _helper(job):
    rc, _, _ = compose.run(["docker", "image", "inspect", storage.FILES_IMAGE], timeout=15)
    if rc:
        job.exec(["docker", "pull", storage.FILES_IMAGE])


def _flushes(config, rows):
    commands = {}
    for row in rows:
        if row["state"] != "running":
            continue
        text = config["services"][row["service"]].get("labels", {}).get("io.gravitee.snapshot.flush")
        if text:
            command = json.loads(text)
            if not isinstance(command, list) or not command or not all(isinstance(word, str) and "\0" not in word for word in command) or not command[0]:
                raise ValueError("A snapshot flush command must be a JSON array of arguments")
            commands[row["id"]] = command
    return commands


def _inherit_flushes(config, inspected):
    # Older runtime records predate flush metadata. Inherit only a core hook for
    # the exact image already running, without rewriting the source's saved config.
    core = compose.config_json(compose.all_files([]), timeout=30)
    for container in inspected:
        key = container["Config"]["Labels"]["com.docker.compose.service"]
        labels = config["services"][key].setdefault("labels", {})
        candidate = core["services"].get(key, {})
        hook = candidate.get("labels", {}).get("io.gravitee.snapshot.flush")
        if hook and not labels.get("io.gravitee.snapshot.flush"):
            rc, image, _ = compose.run(["docker", "image", "inspect", "--format", "{{.Id}}", candidate["image"]], timeout=15)
            if rc or image.strip() != container["Image"]:
                raise ValueError("Apply this runtime’s settings before cloning: its flush metadata and current settings use different images")
            labels["io.gravitee.snapshot.flush"] = hook


def _paused(ids, action, flush=None):
    paused = []
    flush = flush or {}
    try:
        for cid in ids:
            if cid in flush:
                continue
            _execute(["docker", "pause", cid], timeout=30)
            paused.append(cid)
        # Application writers are paused first. Datastores can then commit their
        # acknowledged in-memory writes before their own processes are frozen.
        for cid in ids:
            if cid in flush:
                _execute(["docker", "exec", cid, *flush[cid]], timeout=120)
                _execute(["docker", "pause", cid], timeout=30)
                paused.append(cid)
        action()
    finally:
        failed = []
        for cid in reversed(paused):
            rc, _, _ = compose.run(["docker", "unpause", cid], timeout=30)
            if rc:
                failed.append(cid)
        if failed:
            raise JobFailed("Could not resume the source containers after copying; unpause " + ", ".join(failed))


def _volume(source, target, key):
    destination = independent.name(target) + "_" + key
    rc, _, _ = compose.run(["docker", "volume", "inspect", destination], timeout=15)
    if not rc:
        raise ValueError("The new runtime's destination volume already exists; nothing was overwritten")
    _execute(["docker", "volume", "create", "--label", storage.PROJECT + "=" + independent.name(target),
              "--label", "com.docker.compose.volume=" + key, destination], timeout=30)
    _execute(storage.copy_command(source, destination))


def _pin(target, key, service, image=None):
    image = image or storage.inspect("image", [service["image"]])[0]["Id"]
    tag = independent.name(target) + "/" + key + ":clone"
    _execute(["docker", "image", "tag", image, tag], timeout=30)
    return tag


def _check_canceled(job):
    if job._canceled:
        raise JobFailed("Canceled")


def _files(source, destination):
    if source.is_symlink():
        raise ValueError("A runtime data folder contains a link")
    if not source.is_dir():
        before = runtime_storage.tree(source)["fingerprint"]
        independent._copy(source, destination)
        if runtime_storage.tree(source)["fingerprint"] != before:
            raise ValueError("The source files changed while copying; retry with a new runtime")
        return
    before = runtime_storage.tree(source)["fingerprint"]
    # Reject links, pipes and devices before a root helper copies a writable directory.
    for parent, dirs, files in os.walk(source):
        for name in [*dirs, *files]:
            file = Path(parent) / name
            if file.is_symlink() or not (file.is_dir() or file.is_file()):
                raise ValueError("A runtime data folder contains a link, device, socket or pipe")
    destination.mkdir(parents=True, mode=0o700)
    _execute(storage.copy_command(str(source), str(destination), "bind"))
    if runtime_storage.tree(source)["fingerprint"] != before:
        raise ValueError("The source files changed while copying; retry with a new runtime")


def _volumes(config, inspected, resources):
    """Include image-declared anonymous volumes, but never adopt an external volume."""
    volumes = {row["name"]: row["key"] for row in resources["volumes"]}
    ids = {row["id"] for row in resources["containers"]}
    if None in volumes.values() or len(set(volumes.values())) != len(volumes):
        raise ValueError("Runtime volumes have ambiguous storage identities")
    for row in resources["volumes"]:
        users = set(storage.ask("container", "ls", "-a", "-q", "--no-trunc", "--filter", "volume=" + row["name"]).split())
        if row["driver"] != "local" or row["options"] or not users <= ids:
            raise ValueError("A runtime volume is not exclusive local storage; cloning is held")
        config.setdefault("volumes", {}).setdefault(row["key"], {})
    for container in inspected:
        key = container["Config"]["Labels"]["com.docker.compose.service"]
        for mount in container.get("Mounts", []):
            if mount["Type"] != "volume":
                continue
            declared = next((entry for entry in config["services"][key].get("volumes", []) if entry["target"] == mount["Destination"]), None)
            if mount["Name"] not in volumes:
                if declared:
                    raise ValueError("A declared volume is outside this runtime's ownership; cloning is held")
                spec = storage.inspect("volume", [mount["Name"]])[0]
                users = set(storage.ask("container", "ls", "-a", "-q", "--no-trunc", "--filter", "volume=" + mount["Name"]).split())
                if spec.get("Driver") != "local" or spec.get("Options") or not users <= ids:
                    raise ValueError("An anonymous volume is not exclusive local storage; cloning is held")
                volumes[mount["Name"]] = "clone-" + key + "-" + hashlib.sha256(mount["Destination"].encode()).hexdigest()[:8]
            if not declared:
                volume = volumes[mount["Name"]]
                config.setdefault("volumes", {})[volume] = {}
                config["services"][key].setdefault("volumes", []).append({"type": "volume", "source": volume,
                    "target": mount["Destination"], "read_only": not mount["RW"]})
    return volumes


def _same_paused_resources(before, after):
    expected, observed = copy.deepcopy(before), copy.deepcopy(after)
    ports = {row["id"]: row["ports"] for row in expected["containers"]}
    for row in expected["containers"]:
        if row["state"] == "running":
            row["state"] = "paused"
    for row in observed["containers"]:
        if row["state"] == "paused" and row["id"] in ports:
            # Engine omits published ports from paused containers' NetworkSettings.
            # Their immutable container identity and verified project must still match.
            row["ports"] = ports[row["id"]]
    return observed == expected


def _snapshot(job, project, source, target, config):
    source_config = copy.deepcopy(config)
    private = runtime_settings.mode(source) == "independent"
    original = independent.read(source) if private else None
    root = runtime_storage.directory(source) if private else paths.ROOT
    new = independent.folder(target)
    rows = _running(source, config)
    new.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Freeze the running images, even when the source's mutable local tag changes later.
    inspected = storage.inspect("container", [row["id"] for row in rows["containers"]])
    files = independent.command(source)[len(compose.COMPOSE):] if private else ["-f", paths.CORE_FILE, "-f", paths.ON_FILE]
    hashes = compose.config_hashes(files)
    if hashes is None or any(hashes.get(row["Config"]["Labels"]["com.docker.compose.service"])
                             != row["Config"]["Labels"].get("com.docker.compose.config-hash") for row in inspected):
        raise ValueError("The source configuration differs from its containers; apply its settings before cloning")
    _inherit_flushes(config, inspected)
    volumes = _volumes(config, inspected, rows)
    flushes = _flushes(config, rows["containers"])
    writable = [mount["source"] for service in config["services"].values() for mount in service.get("volumes", [])
                if mount["type"] == "bind" and not mount.get("read_only")]
    if storage.bind_writers(writable) - {row["id"] for row in rows["containers"]}:
        raise ValueError("A runtime file is writable by another container; its clone cannot be isolated safely")
    actual = {row["Config"]["Labels"]["com.docker.compose.service"]: row["Image"] for row in inspected}
    for key, service in config["services"].items():
        if key not in actual:
            actual[key] = storage.inspect("image", [service["image"]])[0]["Id"]
        service["image"] = _pin(target, key, service, actual[key])
        service.pop("build", None)
        service["pull_policy"] = "never"
    if private:
        used = {int(p) for _, value in runtime_storage.known()[0] for p in value.get("ports", {}).values()}
        translate = {str(old): independent._port(used) for old in set(original["ports"].values())}
        ports = {key: translate[str(old)] for key, old in original["ports"].items()}
        config = independent.rewrite(config, translate)
        config["name"] = independent.name(target)
        for service in config["services"].values():
            for port in service.get("ports", []):
                port["published"] = str(translate[str(port["published"])])
            for mount in service.get("volumes", []):
                if mount["type"] == "bind":
                    path = Path(mount["source"])
                    if not path.is_relative_to(root):
                        raise ValueError("A source runtime mount escapes its private directory")
                    mount["source"] = str(new / path.relative_to(root))
        for kind in ("secrets", "configs"):
            for spec in config.get(kind, {}).values():
                if spec.get("file"):
                    spec["file"] = str(new / Path(spec["file"]).relative_to(root))
    else:
        gathered = standalone._gather(source)
        nodes = set(gathered["nodes"])
        for key in config["services"]:
            owner = project["services"].get(key, {}).get("owner") or {}
            if owner.get("kind") in gathered["units"] and owner.get("id") in gathered["units"][owner["kind"]]:
                nodes.add((owner["kind"], owner["id"]))
        gathered["nodes"] = sorted(nodes)
        config, ports, binds = independent._config(target, gathered, None, dry_run=True, snapshot=config)
        translate = ports
        original = independent._record(target, gathered, config, ports, binds)
    value = copy.deepcopy(original)
    value.update(id=runtime_settings.key(target), project=independent.name(target), label=runtime_settings.instances()[target]["name"],
                 ports=ports, active=False, clone_pending=True, cloned_from=source,
                 prepared_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    if private:
        value["binds"] = {key: str(new / Path(path).relative_to(root)) for key, path in value["binds"].items()}
    references = (original.get("snapshot") or {}).get("references") if private else None
    value["snapshot"] = {"scope": "runtime", "images": actual, "references": references or {
        key: service.get("image", "") for key, service in source_config["services"].items()}}
    built = {service.get("image") for service in source_config["services"].values() if service.get("build")}
    value["snapshot"]["local"] = (original.get("snapshot") or {}).get("local", []) if private and original.get("snapshot") else sorted(
        key for key, service in source_config["services"].items() if service.get("build") or service.get("image") in built
        or service.get("pull_policy") == "never" or (value["services"].get(key, {}).get("display") or {}).get("built"))
    for key, service in value["services"].items():
        service["image"] = config["services"][key].get("image", "")
        service["ports"] = [str(port["published"]) for port in config["services"][key].get("ports", [])]
        service["targets"] = {str(port["published"]): str(port["target"]) for port in config["services"][key].get("ports", [])}
    independent._write(new / "runtime.json", value)
    independent._write(new / "compose.json", config)

    def transfer():
        _check_canceled(job)
        # No queued operation or changed ownership can widen the copy after the preview.
        again = storage.resources(source_config["name"])
        if private:
            runtime_storage._owned(source, again)
        if not _same_paused_resources(rows, again):
            raise ValueError("The source runtime changed while preparing its clone")
        if _configuration(source) != source_config:
            raise ValueError("The source configuration changed while preparing its clone")
        if private:
            files = [(root / name, new / name) for name in ("inputs", "mounts") if (root / name).exists()]
        else:
            files = [(Path(real), Path(destination)) for real, destination in value["binds"].items()]
            for kind in ("configs", "secrets"):
                for key, spec in config.get(kind, {}).items():
                    if spec.get("file"):
                        files.append((Path(source_config[kind][key]["file"]).resolve(), Path(spec["file"])))
        for origin, destination in files:
            if any(parent != origin and parent in origin.parents and destination == copy / origin.relative_to(parent)
                   for parent, copy in files):
                continue   # Overlapping mounts must remain views of the same copied directory.
            if origin.exists():
                _files(origin, destination)
            else:
                destination.mkdir(parents=True, mode=0o700)
        for volume, key in volumes.items():
            _check_canceled(job)
            _volume(volume, target, key)

    job.step("Copy runtime storage; briefly pause its writers")
    _paused([row["id"] for row in rows["containers"] if row["state"] == "running"], transfer, flushes)
    # Captured browser URLs in configuration files follow the clone's ports too.
    for file in (new / "inputs").rglob("*"):
        if file.is_file() and file.stat().st_size <= 16 * 1024 * 1024:
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            changed = independent.rewrite(text, translate)
            if changed != text:
                file.write_text(changed, encoding="utf-8")
    return value


def create(project, source, target):
    config = check(source)

    def run(job):
        # Record the guard before copying even the first input. A failed compiler
        # must leave a deletable, unusable clone instead of an ordinary fresh child.
        independent._write(independent.folder(target) / "runtime.json", {
            "schema": 1, "id": runtime_settings.key(target), "playground": runtime_settings.source(target),
            "project": independent.name(target), "clone_pending": True, "cloned_from": source,
            "initialized": False, "active": False, "ports": {}, "binds": {}, "services": {}, "oneshots": [],
            "catalogs": [], "units": [], "plugins": {}, "tested": {"versions": {}, "services": {}}})
        _helper(job)
        _check_canceled(job)
        check(source)
        # The destination never starts during a snapshot, including after failures.
        value = _snapshot(job, project, source, target, config)
        _check_canceled(job)
        value.update(clone_pending=False, initialized=True, active=False)
        independent._write(independent.folder(target) / "runtime.json", value)
        job.lines.append("Created a stopped independent clone; its source and other playgrounds were preserved")
    return JOBS.start("Clone runtime", [run], key=f"playground:{target}", verb="create")
