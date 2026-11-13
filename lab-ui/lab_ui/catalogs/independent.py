"""One private Compose project per named instance, assembled from its playground's dependency closure.

Catalog source stays in place. Writable binds, Compose volumes, networks, built image
names and published ports belong to the runtime. Its generated configuration is private
and fixed until an explicit rebuild; stop keeps every runtime's data and seed record.
"""

import copy
import hashlib
import json
import os
import re
import shutil
import socket
import stat
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .. import paths, products
from ..docker import compose, images
from ..docker.jobs import JOBS, JobFailed
from ..library import layers, model, plugins, runtime_settings, tested
from . import service as catalog_service, standalone

FOLDER = ".lab/runtimes"
LOCK = threading.RLock()
LOCAL_URL = re.compile(r"(?P<host>https?://(?:localhost|127\.0\.0\.1)):(?P<port>\d+)(?!\d)")


def folder(pid):
    if not paths.is_id(pid):
        raise paths.NotFound("No such runtime")
    return paths.ROOT / FOLDER / hashlib.sha256(runtime_settings.key(pid).encode()).hexdigest()[:24]


def name(pid):
    digest = hashlib.sha256((str(paths.ROOT.resolve()) + "\0" + runtime_settings.key(pid)).encode()).hexdigest()[:20]
    return "gravitee-pg-" + digest


def read(pid):
    file = folder(pid) / "runtime.json"
    if not file.exists():
        return None
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("The independent runtime record cannot be read") from error
    if not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1 or value.get("id") != runtime_settings.key(pid) or value.get("project") != name(pid):
        raise ValueError("The independent runtime record does not match this runtime")
    return value


def _write(file, value):
    file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.write_atomic(file, json.dumps(value, indent=2) + "\n")
    file.chmod(0o600)


def command(pid, *args):
    # Both -f and -p are explicit: no command can fall back to the shared Lab project.
    return [*compose.COMPOSE, "--project-directory", str(folder(pid)), "-f", str(folder(pid) / "compose.json"),
            "-p", name(pid), *args]


def _port(used):
    for _ in range(100):
        with socket.socket() as tcp, socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
            tcp.bind(("127.0.0.1", 0))
            port = tcp.getsockname()[1]
            if port in used:
                continue
            try:
                udp.bind(("127.0.0.1", port))
            except OSError:
                continue
            used.add(port)
            return port
    raise ValueError("No free loopback port could be assigned")


def rewrite(value, ports):
    if isinstance(value, dict):
        return {key: rewrite(item, ports) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item, ports) for item in value]
    if isinstance(value, str):
        return LOCAL_URL.sub(lambda match: match["host"] + ":" + str(ports.get(match["port"], match["port"])), value)
    return value


def _copy(source, target):
    if source.is_symlink():
        source = source.resolve(strict=True)
    if source.is_dir():
        # A writable link must never lead back to a source repository or another runtime.
        for parent, dirs, files in os.walk(source, followlinks=False):
            for entry in [*dirs, *files]:
                item = Path(parent) / entry
                if item.is_symlink():
                    raise ValueError("A writable runtime folder contains a link; use a dedicated data folder")
                if not item.is_dir() and not item.is_file():
                    raise ValueError("A runtime cannot copy a socket, device or pipe")
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(".git"))
    elif source.is_file() and stat.S_ISREG(source.stat().st_mode):
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise ValueError("A runtime bind must name an existing file or folder")


def _mount(source, target, empty=False, refresh=False):
    if target.exists() and refresh:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
    if target.exists():
        return
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if empty:
        target.mkdir(mode=0o700)
    else:
        _copy(source, target)


def _requirements(gathered):
    required = (gathered["unit"].get("requires") or {}).get("images") or {}
    if required:
        for component in images.components(gathered["found"]):
            wanted = required.get(component["id"])
            if wanted:
                actual = gathered["found"]["services"][component["services"][0]]["tag"]
                if not layers.enough(gathered["unit"], component["id"], actual, wanted):
                    raise ValueError(f"Choose {component['id']} {wanted} or later on Images before preparing this runtime")
    units = {uid: gathered.get("units", {}).get(kind, {}).get(uid, gathered["unit"] if uid == gathered["unit"]["id"] else {})
             for kind, uid in gathered["nodes"]}
    unset = layers._unset({"addons": {uid: {**unit, "settings": unit.get("settings", [])} for uid, unit in units.items()}})
    if unset:
        raise ValueError("Set the required settings first: " + ", ".join(sorted({key for names in unset.values() for key in names})))


def _config(pid, gathered, previous, dry_run=False):
    _requirements(gathered)
    config = compose.config_json(compose.all_files([gathered["unit"]["file"]]), timeout=60)
    selected = standalone._keep(config, gathered, standalone._products(gathered))
    config["services"] = {key: value for key, value in config["services"].items() if key in selected}
    standalone._without(config["services"], set(gathered["found"]["services"]) - selected)
    config["name"] = name(pid)
    config.pop("include", None)
    ports = copy.deepcopy((previous or {}).get("ports", {}))
    used = {int(port) for port in ports.values()}
    # Reserve different assignments across stopped runtimes too, so their URLs stay stable.
    for file in (paths.ROOT / FOLDER).glob("*/runtime.json"):
        value = json.loads(file.read_text(encoding="utf-8"))
        used.update(int(port) for port in value.get("ports", {}).values())
    binds, built = {}, {}
    allowed = [gathered["folder"].resolve(), (paths.ROOT / "certs").resolve(), (paths.ROOT / "plugins").resolve()]
    for kind, uid in gathered["nodes"]:
        allowed.append((paths.ROOT / paths.unit_file(kind, uid)).parent.resolve())
    for service_name, service in config["services"].items():
        old_name = service.pop("container_name", None)
        if (service.get("network_mode") or service.get("volumes_from") or service.get("privileged")
                or service.get("devices") or any(service.get(key) == "host" for key in ("pid", "ipc", "uts"))):
            raise ValueError("Independent runtimes require their own networks and mounts")
        for key, network in (service.get("networks") or {}).items():
            if old_name:
                network = network or {}
                network.setdefault("aliases", []).append(old_name)
                service["networks"][key] = network
        for index, port in enumerate(service.get("ports", [])):
            original = str(port.get("published") or "0")
            if not original.isdigit():
                raise ValueError("Independent runtimes require individual published ports")
            key = original if original != "0" else f"{service_name}:{index}"
            if key not in ports:
                ports[key] = _port(used)
            port.update(host_ip="127.0.0.1", published=str(ports[key]))
        if len(service.get("ports", [])) == 1:
            # A dynamically published source port has no number to replace. Its console
            # declaration still supplies the browser's original URL.
            for label, url in service.get("labels", {}).items():
                if label.startswith("io.gravitee.console.") and label.endswith(".url"):
                    address = urlsplit(url)
                    if address.hostname in ("localhost", "127.0.0.1") and address.port:
                        ports.setdefault(str(address.port), int(service["ports"][0]["published"]))
        mounts = []
        for index, mount in enumerate(service.get("volumes", [])):
            mount = copy.deepcopy(mount)
            if mount["type"] == "bind":
                source = Path(mount["source"])
                if source.name == "docker.sock" or (source.exists() and source.is_socket()):
                    raise ValueError("Independent runtimes cannot mount the host Docker socket")
                real = source.resolve()
                key = hashlib.sha256(str(real).encode()).hexdigest()[:24]
                if mount.get("read_only"):
                    # Configuration and frame scripts contain browser endpoints too. Pin
                    # their inputs and rewrite only the private copy after assigning ports.
                    target = folder(pid) / "inputs" / "binds" / key
                    if not dry_run:
                        _mount(real, target, refresh=True)
                else:
                    if not any(real == root or root in real.parents for root in allowed):
                        raise ValueError("A writable bind is outside the playground and its dependencies")
                    target = folder(pid) / "mounts" / key
                    empty = ".state" in real.parts or not source.exists()
                    if not dry_run:
                        _mount(real, target, empty=empty)
                    binds[str(real)] = str(target)
                binds[str(real)] = str(target)
                mount["source"] = str(target)
                mount["bind"] = {"create_host_path": False}
            elif mount["type"] == "volume" and not mount.get("source"):
                volume = f"data-{service_name}-{index}"
                config.setdefault("volumes", {})[volume] = {}
                mount["source"] = volume
            elif mount["type"] not in ("volume", "tmpfs"):
                raise ValueError("This mount cannot be isolated in a playground runtime")
            mounts.append(mount)
        service["volumes"] = mounts
        if service.get("build"):
            old = service.get("image")
            service["image"] = f"{name(pid)}/{service_name}:local"
            if old:
                built[old] = service["image"]
        service.get("labels", {}).pop("com.docker.compose.project", None)
    for service in config["services"].values():
        if not service.get("build") and service.get("image") in built:
            service["image"] = built[service["image"]]
    for kind in ("networks", "volumes"):
        for spec in config.get(kind, {}).values():
            if isinstance(spec, dict):
                if spec.get("driver_opts") or spec.get("driver") not in (None, "local", "bridge"):
                    raise ValueError("Independent runtimes require local Docker storage and bridge networks")
                spec.pop("name", None)
                spec.pop("external", None)
    for kind in ("secrets", "configs"):
        for key, spec in config.get(kind, {}).items():
            if spec.get("external"):
                raise ValueError("Independent runtimes cannot use external secrets or configs")
            if spec.get("file"):
                target = folder(pid) / "inputs" / kind / key
                if not dry_run:
                    _mount(Path(spec["file"]), target, refresh=True)
                spec["file"] = str(target)
    if not dry_run:
        for file in (folder(pid) / "inputs").rglob("*"):
            if not file.is_file() or file.stat().st_size > 16 * 1024 * 1024:
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            updated = rewrite(text, ports)
            if updated != text:
                file.write_text(updated, encoding="utf-8")
    config = rewrite(config, ports)
    # A seed must address its private platform, never a shared or remote management API.
    aliases = set(config["services"])
    for service in config["services"].values():
        for spec in (service.get("networks") or {}).values():
            aliases.update((spec or {}).get("aliases", []))
    seed = gathered["unit"].get("seed")
    for key, value in (config["services"].get(seed, {}).get("environment") or {}).items():
        if key in products.SEED_MANAGEMENT_URLS and value:
            url = urlsplit(value)
            if url.hostname not in aliases:
                raise ValueError(f"{key} must address a service in the independent runtime")
    return config, ports, binds


def prepare(pid, rebuild=False):
    with LOCK:
        previous = read(pid)
        if previous and not rebuild:
            return previous
        gathered = standalone._gather(runtime_settings.source(pid))
        catalog_service.preflight(gathered["found"], gathered["nodes"])
        if gathered["unit"].get("blocked") or gathered["unit"].get("needs_catalogs"):
            raise ValueError("; ".join((gathered["unit"].get("blocked") or {}).get("reasons", []))
                             or "The playground needs its catalog dependencies before it can run")
        root = folder(pid)
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        config, ports, binds = _config(pid, gathered, previous)
        seed = gathered["unit"].get("seed")
        value = {"schema": 1, "id": runtime_settings.key(pid), "playground": runtime_settings.source(pid),
                 "project": name(pid), "ports": ports, "binds": binds,
                 "seed": seed, "initialized": bool(previous and previous.get("initialized")),
                 "active": bool(previous and previous.get("active")),
                 "label": runtime_settings.instances().get(pid, {}).get("name", gathered["unit"]["name"]),
                 "prepared_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "catalogs": sorted({paths.origin(uid) for _, uid in gathered["nodes"]}),
                 "units": [list(node) for node in gathered["nodes"]],
                 "oneshots": [key for key in config["services"] if gathered["found"]["services"].get(key, {}).get("oneshot")],
                 "services": {key: {"label": service.get("labels", {}).get("io.gravitee.name") or key,
                                     "image": service.get("image", ""),
                                     "display": {field: gathered["found"]["services"].get(key, {}).get(field) for field in model.SERVICE_KEYS},
                                     "ports": [str(port["published"]) for port in service.get("ports", [])],
                                     "targets": {str(port["published"]): str(port["target"]) for port in service.get("ports", [])}}
                              for key, service in config["services"].items()},
                 "plugins": {product: {**copy.deepcopy(gathered["found"]["products"][product]), "names": names}
                             for product, names in (gathered["unit"].get("requires", {}).get("plugins") or {}).items()}}
        versions, judged = {}, {}
        for variable in tested.variables(gathered["unit"]):
            saved = layers.saved_setting(gathered["found"], variable)
            if saved:
                versions[variable] = saved[0]
                if not variable.endswith("_REGISTRY"):
                    service_name = layers.service_of(gathered["found"], variable)[0]
                    if service_name in value["services"]:
                        judged[variable] = service_name
        value["tested"] = {"versions": versions, "services": judged}
        for key in ("clone_pending", "cloned_from"):
            if key in (previous or {}):
                value[key] = previous[key]
        # Compose config already escapes literal dollars for a second read.
        _write(root / "compose.json", config)
        _write(root / "runtime.json", value)
        return value


def containers(pid):
    value = read(pid)
    if not value:
        return {}
    rc, out, err = compose.run([*compose.COMPOSE, "-p", name(pid), "ps", "--all", "--format", "json"],
                               cwd=compose.scratch(), timeout=15)
    if rc:
        raise compose.ComposeError("Docker could not read the independent runtime")
    text = out.strip()
    rows = json.loads(text) if text.startswith("[") else [json.loads(line) for line in text.splitlines() if line]
    return {row["Service"]: row for row in rows}


def describe(pid, rows=None):
    value = read(pid)
    if not value:
        return {"mode": "independent", "prepared": False, "running": False, "on": False, "services": []}
    rows = containers(pid) if rows is None else rows
    running = any(row.get("State") in ("running", "restarting", "paused") for row in rows.values())
    return {"mode": "independent", "prepared": True, "running": running, "on": running or value.get("active", False),
            "services": [{"id": key, "label": service["label"], "image": service["image"],
                          "state": rows.get(key, {}).get("State", "absent"),
                          "health": rows.get(key, {}).get("Health", "")} for key, service in value["services"].items()]}


def _wait(pid, job):
    value = read(pid)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if job._canceled:
            raise JobFailed("Canceled")
        rows = containers(pid)
        good = True
        for key in value["services"]:
            row = rows.get(key, {})
            if key in value["oneshots"]:
                if value["initialized"] and not row:
                    continue   # Container cleanup keeps the completed initialization in its volumes.
                if row.get("State") == "exited" and row.get("ExitCode") not in (None, 0):
                    raise JobFailed(f"{value['services'][key]['label']} failed")
                good &= row.get("State") == "exited" and row.get("ExitCode", 0) == 0
            else:
                if row.get("State") in ("exited", "dead") or row.get("Health") == "unhealthy":
                    raise JobFailed(f"{value['services'][key]['label']} did not start")
                good &= row.get("State") == "running" and row.get("Health") in (None, "", "healthy")
        if good:
            return
        time.sleep(1)
    raise JobFailed("The independent runtime did not become ready within ten minutes")


def _label(pid):
    return runtime_settings.instances().get(pid, {}).get("name", "independent playground")


def start(pid, rebuild=False):
    def run(job):
        job.step("Prepare independent runtime")
        value = prepare(pid, rebuild=rebuild)
        value["active"] = True
        _write(folder(pid) / "runtime.json", value)
        if not value["initialized"] or rebuild:
            for product, target in value["plugins"].items():
                job.step("Prepare required plugins")
                private = value["binds"].get(str((paths.ROOT / target["folder"]).resolve()))
                if not private:
                    raise JobFailed("The product's plugin folder is not mounted in this independent runtime")
                services = {key: {**spec["display"], "image": spec["image"],
                                  "container": f"{value['project']}-{key}-1"} for key, spec in value["services"].items()}
                own_target = {**target, "folder": private,
                              "services": {key: path for key, path in target["services"].items() if key in services}}
                plugin_project = {"services": services, "products": {product: own_target}}
                plugins.required(job, plugin_project, product, target["names"],
                                 lambda keys: job.exec(command(pid, "pull", *keys)))
        if not value["initialized"]:
            job.step("Start independent platform and apps")
            job.exec(command(pid, "up", "-d", "--build"))
        else:
            lasting = [key for key in value["services"] if key not in value["oneshots"]]
            job.step("Resume independent platform and apps")
            job.exec(command(pid, "up", "-d", "--no-deps",
                             *(["--build", "--force-recreate"] if rebuild else ["--no-recreate"]), *lasting))
        _wait(pid, job)
        _tested(pid, value)
        value["initialized"] = True
        _write(folder(pid) / "runtime.json", value)
    return JOBS.start("Start " + _label(pid), [run], key=f"playground:{pid}", verb="start",
                      changes={"start": [["playground", pid]]})


def _tested(pid, value):
    proof = value["tested"]
    identities = {variable: compose.image_key({"image": value["services"][key]["image"],
                  "container": f"{value['project']}-{key}-1"}) for variable, key in proof["services"].items()}
    tested.remember(pid, proof["versions"], identities)


def stop(pid, remove=False):
    value = read(pid)
    if not value:
        raise ValueError("This independent playground has not been started")
    def run(job):
        if remove and value["seed"]:
            job.step("Delete what its seed created")
            job.exec(command(pid, "run", "--rm", "--no-deps", value["seed"], "--remove"))
            value["initialized"] = False
            _write(folder(pid) / "runtime.json", value)
        job.step("Stop independent platform and apps")
        job.exec(command(pid, "stop"))
        value["active"] = False
        _write(folder(pid) / "runtime.json", value)
    return JOBS.start("Stop " + _label(pid), [run], key=f"playground:{pid}", verb="stop",
                      changes={"stop": [["playground", pid]]})


def reseed(pid):
    value = read(pid)
    if not value or not value["seed"] or not describe(pid)["running"]:
        raise ValueError("Start this independent playground before re-seeding")
    return JOBS.start("Re-seed " + _label(pid), [command(pid, "run", "--rm", "--no-deps", value["seed"]),
                      lambda job: _tested(pid, value)],
                      key=f"playground:{pid}", verb="reseed")


def created(pid):
    value = read(pid)
    if not value:
        return []
    source = (paths.ROOT / paths.unit_folder("playground", runtime_settings.source(pid)) / ".state").resolve()
    target = value["binds"].get(str(source))
    if not target:
        return []
    file = Path(target) / "created.json"
    if not file.exists():
        return []
    if file.is_symlink() or not file.resolve().is_relative_to(folder(pid).resolve()) or file.stat().st_size > 1 << 20:
        raise ValueError("The independent seed record is not a regular runtime file")
    rows = json.loads(file.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("The independent seed record is damaged")
    counts = {}
    for row in rows:
        label = row.get("label") if isinstance(row, dict) else None
        count = row.get("count", 1) if isinstance(row, dict) else None
        if isinstance(label, str) and row.get("id") and type(count) is int and count > 0:
            counts[label] = counts.get(label, 0) + count
    return [{"what": label, "count": count} for label, count in counts.items()]


def plan(pid, rebuild=False):
    gathered = standalone._gather(runtime_settings.source(pid))
    catalog_service.preflight(gathered["found"], gathered["nodes"])
    if gathered["unit"].get("blocked") or gathered["unit"].get("needs_catalogs"):
        raise ValueError("; ".join((gathered["unit"].get("blocked") or {}).get("reasons", []))
                             or "The playground needs its catalog dependencies before it can run")
    previous = read(pid)
    config = _config(pid, gathered, previous, dry_run=True)[0] if not previous or rebuild else json.loads(
        (folder(pid) / "compose.json").read_text(encoding="utf-8"))
    return {"starts": [{"kind": "service", "id": key, "name": service.get("labels", {}).get("io.gravitee.name") or key}
                       for key, service in config["services"].items()],
            "stops": [], "turns_off": [], "restarts": [], "keeps": [], "independent": True}


def service(pid, service_name, operation):
    value = read(pid)
    if not value or service_name not in value["services"]:
        raise paths.NotFound("No such independent service")
    if operation not in ("start", "stop", "restart"):
        raise ValueError("Choose Start, Stop or Restart")
    if service_name in value["oneshots"] and operation != "stop":
        raise ValueError("Use Re-seed to run the playground seed")
    def run(job):
        if operation != "stop":
            value["active"] = True
            _write(folder(pid) / "runtime.json", value)
        job.exec(command(pid, operation, service_name))
    return JOBS.start(f"{operation.capitalize()} {value['services'][service_name]['label']}",
                      [run], key=f"service:{service_key(pid, service_name)}", verb=operation)


def service_key(pid, service_name):
    return "runtime-" + hashlib.sha256(pid.encode()).hexdigest()[:16] + "--" + service_name


def resolve_service(key):
    if not isinstance(key, str) or not key.startswith("runtime-"):
        return None
    for file in (paths.ROOT / FOLDER).glob("*/runtime.json"):
        value = json.loads(file.read_text(encoding="utf-8"))
        pid = runtime_settings.alias(value.get("id"))
        if not paths.is_id(pid) or read(pid) != value:
            raise ValueError("An independent runtime record is damaged")
        for service_name in value["services"]:
            if service_key(pid, service_name) == key:
                return pid, service_name
    raise paths.NotFound("No such independent service")
