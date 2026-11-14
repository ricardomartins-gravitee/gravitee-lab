"""Docker resource ownership and disk usage, read without exposing container environments.

Cleanup callers receive immutable container/network identities and volume creation evidence,
never a project-wide prune command. File removal uses one fixed helper with a single scoped bind.
"""

import csv
import io
import json
import re
from pathlib import Path

from . import compose

FILES_IMAGE = "busybox:1.37.0"
PROJECT = "com.docker.compose.project"
UNITS = {"B": 1,
         **{unit: 1000 ** power for power, unit in enumerate(["kB", "MB", "GB", "TB", "PB"], 1)},
         **{unit: 1024 ** power for power, unit in enumerate(["KiB", "MiB", "GiB", "TiB", "PiB"], 1)}}
SIZE = re.compile(r"\s*(\d+(?:\.\d+)?)\s*([A-Za-z]+)")


def size(text):
    """Bytes in Docker's decimal or binary human-readable sizes; zero for an unrecognized value."""
    found = SIZE.match(text or "")
    return int(round(float(found.group(1)) * UNITS[found.group(2)])) if found and found.group(2) in UNITS else 0


def ask(*args):
    rc, out, err = compose.run(["docker", *args], timeout=60)
    if rc:
        raise compose.ComposeError("Docker could not read runtime storage: " + (err.strip().splitlines() or ["no answer"])[-1])
    return out.strip()


def inspect(kind, names):
    return json.loads(ask(kind, "inspect", *names)) if names else []


def resources(project):
    found = {}
    for kind in ("container", "network", "volume"):
        names = ask(kind, "ls", *( ["-a"] if kind == "container" else []), "-q", "--filter", f"label={PROJECT}={project}").split()
        rows = inspect(kind, names)
        if kind == "container":
            found["containers"] = [{"id": row["Id"], "name": row["Name"].lstrip("/"),
                "state": "paused" if row["State"].get("Paused") else row["State"]["Status"],
                "project": row["Config"].get("Labels", {}).get(PROJECT),
                "directory": row["Config"].get("Labels", {}).get(PROJECT + ".working_dir"),
                "service": row["Config"].get("Labels", {}).get("com.docker.compose.service"),
                "ports": sorted({str(port["HostPort"]) for ports in row.get("NetworkSettings", {}).get("Ports", {}).values()
                                 for port in ports or []})} for row in rows]
        elif kind == "network":
            found["networks"] = [{"id": row["Id"], "name": row["Name"], "project": (row.get("Labels") or {}).get(PROJECT),
                "key": (row.get("Labels") or {}).get("com.docker.compose.network"),
                "containers": sorted(row.get("Containers") or {})} for row in rows]
        else:
            found["volumes"] = [{"name": row["Name"], "project": (row.get("Labels") or {}).get(PROJECT),
                "key": (row.get("Labels") or {}).get("com.docker.compose.volume"), "created": row.get("CreatedAt"),
                "driver": row.get("Driver"), "options": row.get("Options") or {}} for row in rows]
    return {key: sorted(rows, key=lambda row: row["name"]) for key, rows in found.items()}


def usage(fresh=False):
    """Volumes are exclusive per runtime; images and caches are machine-wide, without double-counting shared layers."""
    def read():
        detail = json.loads(ask("system", "df", "--verbose", "--format", "{{json .}}"))
        rc, out, err = compose.run(["docker", "system", "df", "--format", "{{json .}}"], timeout=60)
        if rc:
            raise compose.ComposeError("Docker could not read image and cache usage")
        summary = [json.loads(line) for line in out.splitlines() if line]
        return {"volumes": {row["Name"]: size(row["Size"]) if SIZE.match(row.get("Size", "")) else None
                            for row in detail.get("Volumes") or []},
                "shared": [{"kind": row["Type"], "bytes": size(row["Size"])} for row in summary
                           if row.get("Type") in ("Images", "Build Cache")]}
    if fresh:
        compose.CACHE.drop("local:runtime-storage-usage")
    return compose.CACHE.get("local:runtime-storage-usage", 10, read)


def remove_files_command(folder, manifest):
    mount = io.StringIO()
    csv.writer(mount, lineterminator="").writerow(["type=bind", f"source={folder}", "target=/runtime"])
    return ["docker", "run", "--rm", "--network", "none", "--read-only", "--cap-drop", "ALL",
            "--cap-add", "DAC_OVERRIDE", "--cap-add", "FOWNER", "--security-opt", "no-new-privileges", "--user", "0:0",
            "--mount", mount.getvalue(), "--entrypoint", "sh", FILES_IMAGE,
            '-c', 'test "$(sha256sum /runtime/runtime.json | cut -d " " -f1)" = "$1" || exit 21; find /runtime -mindepth 1 -delete',
            "runtime-cleanup", manifest]


def load(names):
    if not names:
        return {}
    rows = ask("stats", "--no-stream", "--format", "{{json .}}", *names).splitlines()
    return {row["Name"]: {"cpu_percent": float(row.get("CPUPerc", "0%").rstrip("%") or 0),
                          "memory_bytes": size(row.get("MemUsage", "0B").split("/")[0])}
            for row in (json.loads(line) for line in rows)}


def copy_command(source, target, kind="volume"):
    """Copy into new, empty storage. The source is read-only, including during failures."""
    mounts = []
    for value, destination, readonly in ((source, "/from", True), (target, "/to", False)):
        mount = io.StringIO()
        csv.writer(mount, lineterminator="").writerow([f"type={kind}", f"source={value}", f"target={destination}",
                                                    *(["readonly"] if readonly else [])])
        mounts += ["--mount", mount.getvalue()]
    return ["docker", "run", "--rm", "--network", "none", "--read-only", "--security-opt", "no-new-privileges",
            *mounts, "--entrypoint", "sh", FILES_IMAGE, "-c",
            'test -z "$(ls -A /to)" || exit 21; cp -a /from/. /to/']


def bind_writers(folders):
    """Containers writing overlapping host folders, without reading their environments."""
    found = set()
    ids = ask("container", "ls", "-a", "-q", "--no-trunc").split()
    template = '{"id":{{json .Id}},"mounts":{{json .Mounts}}}'
    roots = [Path(folder).resolve() for folder in folders]
    if not roots or not ids:
        return found
    for line in ask("container", "inspect", "--format", template, *ids).splitlines():
        container = json.loads(line)
        for mount in container.get("mounts", []):
            if mount.get("Type") != "bind" or not mount.get("RW"):
                continue
            # Docker Desktop may show its host path with the VM's /host_mnt prefix.
            source = mount["Source"]
            path = Path(source.removeprefix("/host_mnt") if source.startswith("/host_mnt/") else source).resolve()
            if any(path == root or path in root.parents or root in path.parents for root in roots):
                found.add(container["id"])
    return found


def network_users(networks):
    """Network inspect omits containers that have been created but never started; their configuration still refers to it."""
    found = {row["id"]: set() for row in networks}
    names = ask("container", "ls", "-a", "-q", "--no-trunc").split()
    if not names or not networks:
        return found
    template = '{"id":{{json .Id}},"mode":{{json .HostConfig.NetworkMode}},"networks":{{json .NetworkSettings.Networks}}}'
    for line in ask("container", "inspect", "--format", template, *names).splitlines():
        row = json.loads(line)
        used = row.get("networks") or {}
        references = {row.get("mode"), *used, *(spec.get("NetworkID") for spec in used.values())} - {None, ""}
        for network in networks:
            if references & {network["id"], network["name"]} or any(
                    len(key) >= 12 and network["id"].startswith(key) for key in references):
                found[network["id"]].add(row["id"])
    return found
