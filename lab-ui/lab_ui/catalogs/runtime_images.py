"""The Images page's components and version choices for a private Compose project."""

import copy
import hashlib
import json

from .. import paths
from ..docker import compose, images
from ..docker.jobs import JOBS
from ..library import layers, runtime_settings
from . import independent

RUNNING = ("running", "restarting")


def _context(project, pid):
    if runtime_settings.mode(pid) != "independent":
        raise paths.NotFound("No such sandbox")
    unit = layers.lookup(project, "playground", runtime_settings.source(pid))
    value = independent.read(pid)
    if not value or not value.get("initialized") or value.get("clone_pending"):
        raise ValueError("Start this sandbox successfully before changing its versions")
    config = independent._configuration(pid)
    local = set((value.get("snapshot") or {}).get("local", []))
    captured = {}
    for key, service in config["services"].items():
        if key not in value["services"] or key in local or key in value["oneshots"] or service.get("build"):
            continue
        reference = independent.image_reference(value, key, service.get("image"))
        if service.get("pull_policy") == "never" and reference == service.get("image"):
            continue
        if images.tagged(reference):
            captured[key] = reference
    components = {}
    for component in images.components(project):
        keys = [key for key in component["services"] if key in captured]
        if not keys:
            continue
        # Distinct configured values have distinct controls, even if the catalog uses the same variable.
        versions = {images.tagged(captured[key])[1] for key in keys}
        if len(versions) == 1:
            components[component["id"]] = (component, keys)
        else:
            for key in keys:
                components[f"{component['id']}:{key}"] = (component, [key])
    return unit, value, config, captured, components


def _rows(pid):
    try:
        return independent.containers(pid)
    except compose.ComposeError:
        return {}


def state(project, pid):
    _, value, _, captured, components = _context(project, pid)
    local = compose.image_ids()
    rows = _rows(pid)
    result = []
    for key, (component, names) in components.items():
        configured = sorted({compose.reference(captured[name]) for name in names})
        running = sorted({compose.reference(reference) for name in names if rows.get(name, {}).get("State") in RUNNING
                          for reference in [independent.image_reference(value, name, rows[name].get("Image"))] if reference})
        version = images.tagged(captured[names[0]])[1]
        source = images._locate(images.tagged(captured[names[0]])[0])[0] or "Docker Hub"
        result.append({**component, "id": key, "version_key": key, "version": version, "version_resolved": version,
                       "label": ", ".join(dict.fromkeys(value["services"][name]["label"] for name in names)),
                       "registry_key": None, "registry": None, "registry_resolved": None, "source": source, "follows": [],
                       "services": [independent.service_key(pid, name) for name in names], "enabled": True, "outside": None,
                       "configured": configured, "running": running,
                       "pulled": None if local is None else all(ref in local for ref in configured)})
    return {"components": result, "registries": [], "azure_in_use": False}


def tags(project, pid, component):
    _, _, _, captured, components = _context(project, pid)
    if component not in components:
        raise paths.NotFound("No such sandbox image")
    name = components[component][1][0]
    host, repository = images._locate(images.tagged(captured[name])[0])
    return {"tags": images.repository_tags(host, repository), "local": sorted(compose.image_ids() or [])}


def _plan(project, pid, values, *, live):
    unit, value, current, captured, components = _context(project, pid)
    next_config = copy.deepcopy(current)
    for key, tag in values.items():
        if key not in components or not isinstance(tag, str) or not images.TAG.fullmatch(tag):
            raise ValueError(f"Invalid sandbox image version for {key}")
        component, names = components[key]
        required = ((unit.get("requires") or {}).get("images") or {}).get(component["id"])
        if required and not layers.enough(unit, component["id"], tag, required):
            raise ValueError(f"{component['id']} requires {required} or later")
        for name in names:
            repository, previous = images.tagged(captured[name])
            if tag != previous:
                next_config.setdefault(independent.VERSIONS, {})[name] = tag
                service = next_config["services"][name]
                service["image"] = f"{repository}:{tag}"
                if service.get("pull_policy") == "never":
                    service["pull_policy"] = "missing"
    rows = independent.containers(pid) if live else {}
    changed = {name: rows[name].get("ID") for name in next_config.get(independent.VERSIONS, {})
               if name in captured and rows.get(name, {}).get("State") in RUNNING and rows[name].get("Image")
               and compose.reference(rows[name]["Image"]) != compose.reference(next_config["services"][name]["image"])}
    fingerprint = hashlib.sha256(json.dumps([current, next_config, changed], sort_keys=True).encode()).hexdigest()
    preview = {"restarts": [{"kind": "service", "id": name, "name": value["services"][name]["label"]} for name in changed],
               "fingerprint": fingerprint, "runs": bool(changed)}
    return next_config, changed, preview


def save(project, pid, body):
    values = body.get("values", {})
    if not isinstance(values, dict):
        raise ValueError("The versions must be a mapping")
    if body.get("check") is True:
        return _plan(project, pid, values, live=True)[2]
    if body.get("apply") is True:
        if not isinstance(body.get("fingerprint"), str):
            raise ValueError("Review the sandbox version changes before applying them")

        def run(job):
            with independent.LOCK:
                config, changed, preview = _plan(project, pid, values, live=True)
                if body["fingerprint"] != preview["fingerprint"] or not changed:
                    raise ValueError("The sandbox changed after the preview; review the versions again")
                independent._write(independent.folder(pid) / "compose.json", config)
            job.step("Pull sandbox images")
            job.exec(independent.command(pid, "pull", *changed))
            job.step("Recreate changed sandbox containers")
            job.exec(independent.command(pid, "up", "-d", "--no-deps", *changed))
            independent._wait(pid, job, changed)

        return JOBS.start("Apply versions to " + independent._label(pid), [run],
                          key=f"playground:{pid}", verb="apply")

    with independent.LOCK:
        if JOBS.current and not JOBS.current.done:
            raise ValueError("Wait for the current job before saving versions")
        config, _, _ = _plan(project, pid, values, live=False)
        independent._write(independent.folder(pid) / "compose.json", config)
    return state(project, pid)
