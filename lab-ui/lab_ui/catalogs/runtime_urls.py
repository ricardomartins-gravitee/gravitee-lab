"""Relocate local URLs in copied storage using datastore commands declared by Compose.

Helpers see only the new runtime's mounts, with no network or published ports. No
application runs until every helper succeeds; database schemas stay with the store.
"""

import csv
import io
import json
from pathlib import Path

from .. import paths
from ..docker import compose, storage
from ..docker.jobs import JobFailed
from . import independent

def ports(source, destination, inspected, aliases):
    """Original and actually published source ports mapped to their copied ports."""
    found = {str(old): int(new) for old, new in aliases.items() if str(old).isdigit() and int(old)}
    for row in inspected:
        key = row["Config"]["Labels"]["com.docker.compose.service"]
        assigned = destination["services"][key].get("ports", [])
        for before, after in zip(source["services"][key].get("ports", []), assigned):
            target = str(before["target"]) + "/" + before.get("protocol", "tcp")
            for binding in (row.get("NetworkSettings", {}).get("Ports", {}).get(target) or []):
                old, new = str(binding["HostPort"]), int(after["published"])
                if old in found and found[old] != new:
                    raise ValueError("The source's published ports are ambiguous")
                found[old] = new
    return found


def command(value):
    if not isinstance(value, list) or not value or not all(isinstance(word, str) and word and "\0" not in word for word in value):
        raise ValueError("A snapshot relocation command must be a JSON array of arguments")
    return value


def _mount(kind, source, target, readonly=False):
    value = io.StringIO()
    csv.writer(value, lineterminator="").writerow(["type=" + kind, "source=" + source, "target=" + target,
                                                 *(["readonly"] if readonly else [])])
    return ["--mount", value.getvalue()]


def relocate(config, target, mapping, execute):
    """Run declared hooks after source writers resume, on copied mounts only."""
    root, project = independent.folder(target).resolve(), independent.name(target)
    hooks = config.get("x-gravitee", {}).get("snapshot", {})
    for key, service in config["services"].items():
        hook = hooks.get(key, {}).get("relocate")
        if hook is None:
            continue
        hook = command(hook)
        mounts = []
        for mount in service.get("volumes", []):
            kind = mount["type"]
            source = mount.get("source", "")
            if kind == "volume":
                source = project + "_" + source
                spec = storage.inspect("volume", [source])[0]
                if (spec.get("Labels") or {}).get(storage.PROJECT) != project:
                    raise ValueError("URL relocation requires the clone's own storage")
            elif kind == "bind":
                if not Path(source).resolve().is_relative_to(root):
                    raise ValueError("URL relocation requires the clone's own files")
            elif kind == "tmpfs":
                mounts += ["--tmpfs", mount["target"]]
                continue
            else:
                raise ValueError("This storage cannot be used for URL relocation")
            mounts += _mount(kind, source, mount["target"], mount.get("read_only", False))
        mounts += _mount("bind", str(paths.ROOT / "lab-ui/clone"), "/gravitee-lab-clone", True)
        environment = [word for name, value in service.get("environment", {}).items() if value is not None
                       for word in ("--env", name + "=" + str(value))]
        args = ["docker", "create", "--network", "none", "--security-opt", "no-new-privileges", "--pull", "never",
                "--label", storage.PROJECT + "=" + project, *mounts, *environment,
                "--env", "GRAVITEE_LAB_CLONE_PORTS=" + json.dumps(mapping, sort_keys=True),
                "--entrypoint", hook[0], service["image"], *hook[1:]]
        cid = execute(args, timeout=30)
        try:
            execute(["docker", "start", "--attach", cid], timeout=600)
            state = storage.inspect("container", [cid])[0]["State"]
            if state.get("Running") or state.get("ExitCode") != 0:
                raise JobFailed("Could not update local URLs in the clone's copied data")
        finally:
            rc, _, _ = compose.run(["docker", "rm", "--force", cid], timeout=30)
            if rc:
                raise JobFailed("Could not remove the clone's URL relocation helper")
