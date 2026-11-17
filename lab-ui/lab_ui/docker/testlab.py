"""Refuse foreign Docker resources before disposable fixtures start or clean up.

Compose project names alone do not isolate explicit resource names or host binds.
The same checks run again during cleanup, including the resources Docker actually holds.
"""

import json
from pathlib import Path

PROJECT = "com.docker.compose.project"


def within(source, roots):
    path = Path(source).resolve()
    return any(path == root or root in path.parents for root in roots)


def config(value, project, roots):
    roots = tuple(Path(root).resolve() for root in roots)
    if value.get("name") != project:
        raise AssertionError("The fixture must use its own Compose project")
    for kind in ("volumes", "networks"):
        for row in value.get(kind, {}).values():
            if (row.get("external") or not row.get("name", "").startswith(project + "_")
                    or row.get("driver_opts") or row.get("driver") not in (None, "local", "bridge")):
                raise AssertionError("The fixture must use its own local volumes and networks")
    for kind in ("configs", "secrets"):
        for row in value.get(kind, {}).values():
            if row.get("external") or (row.get("file") and not within(row["file"], roots)):
                raise AssertionError("The fixture cannot read foreign configs or secrets")
    for row in value.get("services", {}).values():
        if row.get("container_name") and not row["container_name"].startswith(project + "-"):
            raise AssertionError("The fixture cannot name a foreign container")
        if (row.get("privileged") or row.get("devices") or row.get("volumes_from")
                or row.get("network_mode") or row.get("pid") or row.get("ipc") not in (None, "private", "shareable")):
            raise AssertionError("The fixture cannot share host or foreign container access")
        for mount in row.get("volumes", []):
            if mount.get("type") == "bind" and not within(mount["source"], roots):
                raise AssertionError("The fixture cannot mount a foreign host path")
        build = row.get("build") or {}
        if isinstance(build, dict) and build.get("context") and not within(build["context"], roots):
            raise AssertionError("The fixture cannot build from a foreign host path")


def resources(value, project, roots, run, fresh=False):
    """`run` reads Docker or raises on failure: an inspection error never means absent."""
    config(value, project, roots)
    roots = tuple(Path(root).resolve() for root in roots)
    containers = run(["docker", "container", "ls", "-aq", "--no-trunc", "--filter", "label=" + PROJECT + "=" + project]).split()
    if fresh and containers:
        raise AssertionError("The fixture project already has containers")
    expected = {kind: {row["name"] for row in value.get(kind + "s", {}).values()} for kind in ("volume", "network")}
    for kind in ("volume", "network"):
        all_names = set(run(["docker", kind, "ls", "--format", "{{.Name}}"]).split())
        names = {name for name in all_names if name in expected[kind] or name.startswith(project + "_")}
        if fresh and names:
            raise AssertionError("The fixture would reuse an existing " + kind)
        for name in sorted(names):
            row = json.loads(run(["docker", kind, "inspect", name]))[0]
            if (row.get("Labels") or {}).get(PROJECT) != project:
                raise AssertionError("The fixture cannot clean up a foreign " + kind)
            if kind == "volume" and (row.get("Options") or row.get("Driver") != "local"):
                raise AssertionError("The fixture cannot use host-backed or remote volumes")
            if kind == "network" and set(row.get("Containers") or {}) - set(containers):
                raise AssertionError("The fixture network has foreign containers")
    for cid in containers:
        row = json.loads(run(["docker", "container", "inspect", cid]))[0]
        labels = row["Config"].get("Labels") or {}
        if labels.get(PROJECT) != project or not within(labels.get(PROJECT + ".working_dir", "/"), roots):
            raise AssertionError("The fixture cannot operate on a foreign container")
        for mount in row.get("Mounts", []):
            if mount["Type"] == "bind" and not within(mount["Source"], roots):
                raise AssertionError("The fixture container has a foreign host mount")
            if mount["Type"] == "volume" and mount.get("Name") not in expected["volume"]:
                # Anonymous image volumes belong to this exact container and are removed only with it.
                if not (len(mount.get("Name", "")) == 64 and all(c in "0123456789abcdef" for c in mount["Name"])):
                    raise AssertionError("The fixture container has a foreign named volume")
