"""Create stopped child runtimes without writing to their source or catalog.

Private projects copy only their verified exclusive storage while their writers are
briefly paused. Shared projects export owned resources through the seeder adapter;
shared platform volumes are never copied. Incomplete targets stay stopped and marked
unusable, with their definition retained for scoped cleanup.
"""

import copy
import hashlib
import json
import os
import shutil
import time
import uuid
from pathlib import Path

from .. import paths, products
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
    config = compose.config_json(compose.all_files([gathered["unit"]["file"]]), timeout=60)
    keep = standalone._keep(config, gathered, standalone._products(gathered))
    config["services"] = {key: value for key, value in config["services"].items() if key in keep}
    return config


def _running(pid, config):
    rows = storage.resources(config["name"])
    lasting = {key for key, service in config["services"].items()
               if service.get("restart") != "no" and service.get("labels", {}).get("io.gravitee.role") != "seed"}
    live = {row["service"] for row in rows["containers"] if row["state"] == "running"}
    if not lasting or not lasting <= live:
        raise ValueError("Start the source runtime successfully before cloning it")
    if runtime_settings.mode(pid) == "independent":
        runtime_storage._owned(pid, rows)
    return rows


def check(pid):
    config = _configuration(pid)
    _running(pid, config)
    if runtime_settings.mode(pid) != "independent":
        file = _state(pid) / "created.json"
        if file.exists() or file.is_symlink():
            if file.is_symlink() or not file.is_file() or file.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("The playground's ownership record must be a regular file")
            rows = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(rows, list) or not all(isinstance(row, dict) and isinstance(row.get("product"), str)
                                                   and isinstance(row.get("kind"), str) for row in rows):
                raise ValueError("The playground's ownership record is damaged")
            missing = sorted({products.SEEDED.get(row["product"].split("-")[0], "other") for row in rows
                              if row["kind"] not in products.CLONE_RECORDS.get(row["product"], ())})
            if missing:
                raise ValueError("Shared cloning does not yet support this playground's " + ", ".join(missing)
                                 + " resources; use Fresh from playground")
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
                raise ValueError("Rebuild this runtime before cloning: its flush metadata and current settings use different images")
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
        independent._copy(source, destination)
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


def _private(job, source, target, config):
    original = independent.read(source)
    root, new = runtime_storage.directory(source), independent.folder(target)
    rows = _running(source, config)
    new.mkdir(parents=True, mode=0o700, exist_ok=True)
    # Freeze the running images, even when the source's mutable local tag changes later.
    inspected = storage.inspect("container", [row["id"] for row in rows["containers"]])
    _inherit_flushes(config, inspected)
    volumes = _volumes(config, inspected, rows)
    writable = [mount["source"] for service in config["services"].values() for mount in service.get("volumes", [])
                if mount["type"] == "bind" and not mount.get("read_only")]
    if storage.bind_writers(writable) - {row["id"] for row in rows["containers"]}:
        raise ValueError("A runtime file is writable by another container; its clone cannot be isolated safely")
    actual = {row["Config"]["Labels"]["com.docker.compose.service"]: row["Image"] for row in inspected}
    for key, service in config["services"].items():
        service["image"] = _pin(target, key, service, actual.get(key))
        service.pop("build", None)
        service["pull_policy"] = "never"
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
    value = copy.deepcopy(original)
    value.update(id=runtime_settings.key(target), project=independent.name(target), label=runtime_settings.instances()[target]["name"],
                 ports=ports, active=False, clone_pending=True, cloned_from=source,
                 prepared_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    value["binds"] = {key: str(new / Path(path).relative_to(root)) for key, path in value["binds"].items()}
    for key, service in value["services"].items():
        service["image"] = config["services"][key].get("image", "")
        service["ports"] = [str(port["published"]) for port in config["services"][key].get("ports", [])]
        service["targets"] = {str(port["published"]): str(port["target"]) for port in config["services"][key].get("ports", [])}
    independent._write(new / "runtime.json", value)
    independent._write(new / "compose.json", config)

    def transfer():
        _check_canceled(job)
        # No queued operation or changed ownership can widen the copy after the preview.
        again = storage.resources(original["project"])
        runtime_storage._owned(source, again)
        if not _same_paused_resources(rows, again):
            raise ValueError("The source runtime changed while preparing its clone")
        for name in ("inputs", "mounts"):
            if (root / name).exists():
                if (new / name).exists():
                    shutil.rmtree(new / name)
                _files(root / name, new / name)
        for volume, key in volumes.items():
            _check_canceled(job)
            _volume(volume, target, key)

    job.step("Copy private storage; briefly pause its writers")
    _paused([row["id"] for row in rows["containers"] if row["state"] == "running"], transfer, _flushes(config, rows["containers"]))
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


def _state(pid):
    return (paths.ROOT / paths.unit_folder("playground", pid) / ".state").resolve()


def _foreign(project, pid):
    rows = []
    for other in project["playgrounds"]:
        if other == pid or runtime_settings.mode(other) == "independent":
            continue
        file = _state(other) / "created.json"
        if file.exists():
            value = json.loads(file.read_text(encoding="utf-8"))
            if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
                raise ValueError("Another playground has an unreadable ownership record")
            rows.extend(value)
    return rows


def _adapter(job, config, seed, transaction, operation, private_state):
    name = "lab-clone-" + uuid.uuid4().hex
    service = copy.deepcopy(config["services"][seed])
    service.pop("build", None)
    service.pop("depends_on", None)
    service.pop("container_name", None)
    service.update(entrypoint=["python"], command=["-m", "lab_seeder.runtime_clones", operation, "/checkpoint/resources.json"],
                   working_dir="/clone-code", restart="no")
    state_dir = (service.get("environment") or {}).get("STATE_DIR", "/state")
    service["volumes"] = [{"type": "bind", "source": str(paths.ROOT / "seeder"), "target": "/clone-code", "read_only": True},
                          {"type": "bind", "source": str(transaction), "target": "/checkpoint"},
                          {"type": "bind", "source": str(private_state), "target": state_dir}]
    service["networks"] = {key: {} for key in service.get("networks", {})}
    # Join the already-running source/target networks, without creating or changing them.
    networks = {key: {"external": True, "name": (config.get("networks", {}).get(key) or {}).get("name")
                     or config["name"] + "_" + key} for key in service["networks"]}
    adapter = transaction / "adapter.json"
    independent._write(adapter, {"services": {"clone-adapter": service}, "networks": networks})
    (transaction / "result.json").unlink(missing_ok=True)
    independent._write(transaction / "owner.json", {"uid": os.getuid(), "gid": os.getgid()})
    command = [*compose.COMPOSE, "-f", str(adapter), "-p", name, "run", "-d", "--rm", "--no-deps", "--name", name, "clone-adapter"]
    _execute(command, timeout=120)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = transaction / "result.json"
        if result.is_file() and not result.is_symlink():
            answer = json.loads(result.read_text(encoding="utf-8"))
            if answer.get("ok") is not True:
                raise JobFailed(answer.get("error") or "The playground clone adapter failed")
            return
        rc, out, _ = compose.run(["docker", "inspect", "--format", "{{.State.Running}}", name], timeout=10)
        if rc or out.strip() != "true":
            # Check once more: result and container exit can cross between the two reads.
            if result.exists():
                continue
            raise JobFailed("The playground clone adapter stopped without a result")
        time.sleep(.5)
    raise JobFailed("The playground clone adapter is still running; its transaction was retained")


def _shared(job, project, source, target, config):
    unit = project["playgrounds"][source]
    seed = unit.get("seed")
    value = independent.prepare(target)
    value.update(clone_pending=True, cloned_from=source)
    independent._write(independent.folder(target) / "runtime.json", value)
    transaction = independent.folder(target) / "clone"
    transaction.mkdir(parents=True, mode=0o700)
    private_state = transaction / "source-state"
    private_state.mkdir(mode=0o700)
    for name in ("created.json", "saved-state-subscriptions.json"):
        file = _state(source) / name
        if file.exists():
            if file.is_symlink() or not file.is_file():
                raise ValueError("The playground's ownership record must be a regular file")
            shutil.copy2(file, private_state / name)
    binding = hashlib.sha256((private_state / "created.json").read_bytes()).hexdigest() if (private_state / "created.json").exists() else None
    independent._write(transaction / "foreign.json", _foreign(project, source))
    resources = seed and (private_state / "created.json").exists() and bool(json.loads((private_state / "created.json").read_text(encoding="utf-8")))
    if resources:
        job.step("Export only this playground's resources and consumer access")
        _adapter(job, config, seed, transaction, "export", private_state)
        _check_canceled(job)
    own = {key for key, service in project["services"].items()
           if service.get("owner", {}).get("kind") == "playground" and service["owner"]["id"] == source}
    rows = _running(source, config)
    target_config = json.loads((independent.folder(target) / "compose.json").read_text(encoding="utf-8"))
    actual = {row["Config"]["Labels"]["com.docker.compose.service"]: row["Image"]
              for row in storage.inspect("container", [row["id"] for row in rows["containers"] if row["service"] in config["services"]])}
    for key, service in target_config["services"].items():
        tag = _pin(target, key, config["services"][key], actual.get(key))
        target_config["services"][key]["image"] = tag
        target_config["services"][key].pop("build", None)
        target_config["services"][key]["pull_policy"] = "never"
        value["services"][key]["image"] = tag
    independent._write(independent.folder(target) / "compose.json", target_config)
    members = [row for row in rows["containers"] if row["service"] in own]
    owned_volumes = {row["name"]: row for row in rows["volumes"]}
    app_volumes = {mount["Name"] for row in storage.inspect("container", [row["id"] for row in members])
                   for mount in row.get("Mounts", []) if mount["Type"] == "volume"}
    for name in app_volumes:
        row = owned_volumes.get(name)
        users = set(storage.ask("container", "ls", "-a", "-q", "--no-trunc", "--filter", "volume=" + name).split())
        if not row or row["driver"] != "local" or row["options"] or not users <= {member["id"] for member in members}:
            raise ValueError("An application volume is shared or has ambiguous ownership; cloning is held")

    writable = {mount["source"] for key in own for mount in config["services"].get(key, {}).get("volumes", [])
                if mount["type"] == "bind" and not mount.get("read_only")}
    if storage.bind_writers(writable) - {row["id"] for row in members}:
        raise ValueError("A playground file is writable by another container; its clone cannot be isolated safely")
    home = (paths.ROOT / paths.unit_folder("playground", source)).resolve()

    def transfer():
        _check_canceled(job)
        for real, destination in value["binds"].items():
            path = Path(real)
            # Only the playground's writable files. Dependency and platform storage stays fresh.
            if real in {str(Path(item).resolve()) for item in writable}:
                if path != home and home not in path.parents:
                    raise ValueError("A shared playground writes outside its own folder; that data cannot be scoped safely")
                if Path(destination).exists():
                    if Path(destination).is_dir():
                        shutil.rmtree(destination)
                    else:
                        Path(destination).unlink()
                if path.exists():
                    _files(path, Path(destination))
                else:
                    Path(destination).mkdir(parents=True, mode=0o700)
        for name in app_volumes:
            _check_canceled(job)
            _volume(name, target, owned_volumes[name]["key"])

    job.step("Copy playground application data")
    _paused([row["id"] for row in members if row["state"] == "running"], transfer, _flushes(config, members))
    if resources:
        _check_canceled(job)
        if binding != hashlib.sha256((_state(source) / "created.json").read_bytes()).hexdigest():
            raise ValueError("The source ownership changed during cloning; retry with a new runtime")
        _adapter(job, config, seed, transaction, "verify", private_state)
        target_state = Path(value["binds"][str(_state(source))])
        for file in target_state.glob("*.json"):
            if file.name in ("created.json", "saved-state-subscriptions.json"):
                file.unlink()
        job.step("Start the clone's platform to import its resources")
        lasting = [key for key in value["services"] if key not in value["oneshots"]]
        job.exec(independent.command(target, "up", "-d", "--no-build", "--no-deps", *lasting))
        value["initialized"] = True
        independent._write(independent.folder(target) / "runtime.json", value)
        independent._wait(target, job)
        job.step("Import resources into the independent clone")
        _adapter(job, target_config, seed, transaction, "import", target_state)
    value["initialized"] = True
    return value


def create(project, source, target):
    config = check(source)

    def run(job):
        value = None
        # Record the guard before copying even the first input. A failed compiler
        # must leave a deletable, unusable clone instead of an ordinary fresh child.
        independent._write(independent.folder(target) / "runtime.json", {
            "schema": 1, "id": runtime_settings.key(target), "playground": runtime_settings.source(target),
            "project": independent.name(target), "clone_pending": True, "cloned_from": source,
            "initialized": False, "active": False, "ports": {}, "binds": {}, "services": {}, "oneshots": [],
            "catalogs": [], "units": [], "plugins": {}, "tested": {"versions": {}, "services": {}}})
        try:
            pending = independent.prepare(target, rebuild=True)
            pending.update(clone_pending=True, cloned_from=source)
            independent._write(independent.folder(target) / "runtime.json", pending)
            _helper(job)
            if job._canceled:
                raise JobFailed("Canceled")
            check(source)
            value = _private(job, source, target, config) if runtime_settings.mode(source) == "independent" else _shared(job, project, source, target, config)
            if job._canceled:
                raise JobFailed("Canceled")
        finally:
            # A failed or canceled clone never exposes partially imported data as a usable runtime.
            if (independent.folder(target) / "compose.json").exists():
                _execute(independent.command(target, "stop"), timeout=120)
        transaction = independent.folder(target) / "clone"
        if transaction.exists():
            shutil.rmtree(transaction)
        value.update(clone_pending=False, initialized=True, active=False)
        independent._write(independent.folder(target) / "runtime.json", value)
        job.lines.append("Created a stopped independent clone; its source and other playgrounds were preserved")
    return JOBS.start("Clone runtime", [run], key=f"playground:{target}", verb="create")
