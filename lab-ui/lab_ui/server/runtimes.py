"""Expose private playground runtimes through the existing Lab state and actions.

The shared project keeps its own service keys. Private services get stable UI keys;
commands resolve them back to a verified playground project before reaching Docker.
"""

import copy
import hashlib

from .. import paths
from ..catalogs import independent, runtime_clones, runtime_storage
from ..docker import compose, storage
from ..docker.jobs import JOBS
from ..gravitee import frames
from ..library import layers, model, playgrounds, project as library_project, runtime_settings
from . import personas


def selected(pid):
    return runtime_settings.mode(pid) == "independent"


def project_for(project, pid):
    source = runtime_settings.source(pid)
    if source == pid:
        return project
    unit = layers.lookup(project, "playground", source)
    return {**project, "playgrounds": {**project["playgrounds"], pid: {**unit, "id": pid,
            "name": runtime_settings.instances()[pid]["name"]}}}


def _snapshot(pid):
    def read():
        rows = independent.containers(pid) if independent.read(pid) else {}
        return independent.describe(pid, rows=rows), rows
    return compose.CACHE.get(f"local:independent:{paths.ROOT}:{pid}", 1, read)


def info(pid):
    return _snapshot(pid)[0]


def _states(rows, value, job=None):
    return {key: model.service_state({"state": row.get("State"), "exit_code": row.get("ExitCode"),
                                     "health": row.get("Health")} if row else None, key in value["oneshots"], bool(job and not job.get("done")))
            for key in value["services"] for row in [rows.get(key, {})]}


def _overlay(body, project):
    for item in body.get("playgrounds", []):
        pid = item["id"]
        mode = runtime_settings.mode(pid)
        item["runtime"] = {"mode": mode}
        if selected(pid):
            parent = runtime_settings.source(pid)
            item["runtime"].update(parent=parent, parent_name=project["playgrounds"][parent]["name"])
        if mode != "independent":
            continue
        value = independent.read(pid)
        runtime, rows = _snapshot(pid)
        item["runtime"].update(runtime)
        running, active = runtime["running"], runtime["on"]
        states = _states(rows, value, body.get("job")) if value else {}
        changing = body.get("job") or {}
        verb = changing.get("verb") if changing.get("key") == f"playground:{pid}" and not changing.get("done") else None
        folded = [state for key, state in states.items() if not (value and value.get("initialized")
                  and key in value["oneshots"] and state["key"] == "absent")]
        state = model.fold(folded, active, verb)
        blocked = project["playgrounds"][pid].get("blocked")
        reason = body.get("wait") or ("This clone is incomplete; delete it and create a new runtime" if value and value.get("clone_pending")
                                     else "The playground is blocked" if blocked else None)
        item.update(on=active, up=state["key"] == "running", state=state,
                    note="Independent", note_title="Its own platform, storage and ports", note_tone="",
                    note_target=None, note_file=None, note_causes=[],
                    brings=[], recreates=[], addons=[], setups=[],
                    created=independent.created(pid), secondary={"do": "stop", "label": "Turn off", "enabled": True,
                        "reason": None, "dialog": None} if active and not running else None,
                    action={"do": "stop" if running else "start", "label": "Stop" if running else "Start",
                            "enabled": not reason, "reason": reason, "dialog": None},
                    stop={"stops": [], "turns_off": [], "keeps": [], "delete": {"enabled": running, "reason": None}
                          if project["playgrounds"][pid].get("seed") else None},
                    reseed={"enabled": running and not reason, "reason": reason or (None if running else "Start it first")}
                           if project["playgrounds"][pid].get("seed") else None)
        if active:
            item["held"] = {**(item.get("held") or {}), "delete": "Stop this independent playground first"}
        if value:
            own = []
            for service_name, service in value["services"].items():
                key = independent.service_key(pid, service_name)
                display = service.get("display") or body["services"].get(service_name) or {}
                entry = copy.deepcopy({key: display.get(key) for key in model.SERVICE_KEYS})
                entry.setdefault("ports", [])
                entry.update(uptime=model.uptime(rows.get(service_name, {}).get("Status")),
                             running_tag=model.tag(service["image"]) if states.get(service_name, {}).get("running") else None,
                             label=service["label"], image=service["image"], on=True, behind=False,
                             note="", note_tone="", note_title="", note_target=None, note_file=None,
                             brings=[], recreates=[],
                             container=rows.get(service_name, {}).get("Name") or f"{value['project']}-{service_name}-1",
                             state=states.get(service_name) or model.service_state(None, False, False),
                             owner={"kind": "playground", "id": pid, "name": item["name"]},
                             runtime={"id": pid, "service": service_name})
                entry["ports"] = service.get("ports", [str(value["ports"].get(str(port), port)) for port in entry.get("ports", [])])
                entry["targets"] = {str(value["ports"].get(str(port), port)): target
                                    for port, target in service.get("targets", entry.get("targets", {})).items()}
                entry = independent.rewrite(entry, value["ports"])
                body["services"][key] = entry
                own.append(key)
            item["services"] = own
            item["apps"] = [independent.service_key(pid, key) for key in value["services"] if key not in value["oneshots"]]
            item["links"] = [independent.rewrite(link, value["ports"]) for link in project["links"]
                             if link["service"] in value["services"]]
            for link in item["links"]:
                link["port"] = value["ports"].get(str(link.get("port")), link.get("port"))
                link["up"] = states.get(link["service"], {}).get("running", False)
                link["service"] = independent.service_key(pid, link["service"])
            body.setdefault("links", []).extend({**link, "section": item["name"] + " (independent)"} for link in item["links"])
            tabs = independent.rewrite(frames.declared(project, pid), value["ports"])
            for tab in tabs:
                if tab.get("persona"):
                    tab["persona"] = persona_key(pid, tab["persona"])
            item["tabs"] = [{**tab, "service": independent.service_key(pid, tab["service"]),
                             "up": states.get(tab["service"], {}).get("running", False),
                             "state": states.get(tab["service"]) or model.service_state(None, False, False)} for tab in tabs]
            if item.get("terminal"):
                item["terminal"].update(up=states.get(project["playgrounds"][pid]["terminal"], {}).get("running", False))
        else:
            item.update(links=[], tabs=[], services=[], apps=[])
            if item.get("terminal"):
                item["terminal"]["up"] = False
    return body


def check(project, pid, rebuild=False):
    unit = layers.lookup(project, "playground", runtime_settings.source(pid))
    try:
        dialog = independent.plan(pid, rebuild=rebuild)
        checks = [{"name": "Independent runtime", "state": "ok", "detail": "Own platform, data and ports"}]
    except (ValueError, compose.ComposeError) as error:
        dialog = None
        checks = [{"name": "Independent runtime", "state": "warn", "detail": str(error)}]
    checks += playgrounds._host_checks(unit)
    checks += playgrounds._license_checks(project, unit, {})
    return {"checks": checks, "dialog": dialog}


def create(project, pid, body):
    # A new child is stopped and unprepared: its first Start captures private inputs.
    if JOBS.current and not JOBS.current.done:
        raise RuntimeError("Wait for the current job before creating a runtime")
    layers.lookup(project, "playground", pid)
    if selected(pid):
        raise ValueError("Create runtimes inside the source playground")
    mode = body.get("mode", "fresh")
    if mode not in ("fresh", "clone"):
        raise ValueError("Choose Fresh from playground or Clone runtime")
    source = body.get("source")
    if mode == "clone":
        if not isinstance(source, str) or runtime_settings.source(source) != pid:
            raise ValueError("Clone only a runtime of this playground")
        runtime_clones.check(source)
    independent.plan(pid, rebuild=True)  # Validate the catalog closure before registering anything.
    created = runtime_settings.create(pid, body.get("name"))
    if mode == "clone":
        try:
            created["job"] = runtime_clones.create(project, source, created["id"]).summary()
        except Exception:
            runtime_settings.forget(created["id"])
            raise
    return created


def creation_options(project, pid):
    layers.lookup(project, "playground", pid)
    if selected(pid):
        raise ValueError("Create runtimes inside the source playground")
    sources = [{"id": pid, "name": "Shared"}, *[{"id": rid, "name": row["name"]}
               for rid, row in runtime_settings.instances().items() if row["playground"] == pid]]
    return {"sources": [{**row, "reason": runtime_clones.reason(row["id"]),
                         "private": runtime_settings.mode(row["id"]) == "independent"} for row in sources]}


def action(project, pid, operation, body=None):
    layers.lookup(project, "playground", runtime_settings.source(pid))
    if not selected(pid):
        raise ValueError("This playground uses the shared runtime")
    body = body or {}
    if body.get("place") not in (None, "machine"):
        raise ValueError("An independent runtime runs only on this machine")
    if operation in ("start", "rebuild"):
        if (independent.read(pid) or {}).get("clone_pending"):
            raise ValueError("This clone is incomplete; delete it and create a new runtime")
        result = check(project, pid, rebuild=operation == "rebuild")
        if any(row["state"] == "warn" for row in result["checks"]):
            raise ValueError(next(row["detail"] for row in result["checks"] if row["state"] == "warn"))
        return independent.start(pid, rebuild=operation == "rebuild")
    if operation == "stop":
        return independent.stop(pid, remove=bool(body.get("remove")))
    if operation == "reseed":
        return independent.reseed(pid)
    if operation == "restart":
        if not independent.read(pid):
            raise ValueError("Start this playground first")
        value = independent.read(pid)
        lasting = [key for key in value["services"] if key not in value["oneshots"]]
        return JOBS.start("Restart " + independent._label(pid), [independent.command(pid, "restart", *lasting)], key=f"playground:{pid}", verb="restart")
    raise ValueError("Unknown independent runtime action")


def services(project):
    # Logs and service actions use the same verified container identities as the state.
    body = {"playgrounds": [], "services": copy.deepcopy(project["services"])}
    for pid in runtime_settings.instances():
        if not independent.read(pid):
            continue
        value, rows = independent.read(pid), independent.containers(pid)
        for key in value["services"]:
            ui_key = independent.service_key(pid, key)
            body["services"][ui_key] = {**project["services"].get(key, {}),
                "container": rows.get(key, {}).get("Name") or f"{value['project']}-{key}-1"}
    return body["services"]


def view(project, pid):
    project = project_for(project, pid)
    value = independent.read(pid)
    if not value:
        if selected(pid):
            raise ValueError("Start this runtime before opening its consoles")
        return project
    return independent.rewrite(project, value["ports"])


def persona_key(pid, persona):
    return independent.service_key(pid, hashlib.sha256(persona.encode()).hexdigest()[:16])


def persona_api(persona):
    """Resolve private personas only through declared tabs; an unknown private key never uses the shared API."""
    if not persona.startswith("runtime-"):
        return personas.API
    project = library_project.load()
    for pid in runtime_settings.instances():
        if runtime_settings.source(pid) not in project["playgrounds"]:
            continue
        value = independent.read(pid)
        if not value:
            continue
        if any(tab.get("persona") and persona_key(pid, tab["persona"]) == persona for tab in frames.declared(project, runtime_settings.source(pid))):
            port = value["ports"].get(str(personas.API[1]))
            return (personas.API[0], port) if port else None
    return None


def overlay(body, project):
    for item in [*body.get("addons", []), *body.get("setups", []), *body.get("playgrounds", [])]:
        try:
            held = runtime_settings.holds(item.get("kind", "playground"), item["id"])
        except ValueError as error:
            problem = {"file": independent.FOLDER, "message": str(error), "editable": False}
            if problem not in body.setdefault("problems", []):
                body["problems"].append(problem)
            held = True
        if held:
            item["held"] = {**(item.get("held") or {}), "delete": "Stop the independent playground using this item first"}
    originals = {item["id"]: item for item in body.get("playgrounds", [])}
    try:
        instances = runtime_settings.instances()
    except ValueError as error:
        body.setdefault("problems", []).append({"file": runtime_settings.FILE, "message": str(error), "editable": False})
        return body
    for rid, instance in instances.items():
        parent = originals.get(instance["playground"])
        if parent:
            item = copy.deepcopy(parent)
            item.update(id=rid, name=instance["name"], personal=False, settings=[], deployed=[], deploys_to=[],
                        twin=None, first_start=None)
            body["playgrounds"].append(item)
    for item in body.get("playgrounds", []):
        try:
            _overlay({**body, "playgrounds": [item]}, project_for(project, item["id"]))
        except (OSError, ValueError, KeyError, compose.ComposeError) as error:
            body.setdefault("problems", []).append({"file": runtime_settings.FILE, "message": str(error), "editable": False})
            item["runtime"] = {"mode": "independent", "error": str(error)}
            item["action"] = {"do": "start", "label": "Start", "enabled": False, "reason": str(error), "dialog": None}
            item["reseed"] = None
    return body


def listing(project, fresh=False, playground=None):
    """The Containers page's central inventory, including stopped and unavailable-source runtimes."""
    if playground is not None:
        layers.lookup(project, "playground", playground)
    records, problems = runtime_storage.known()
    prepared = dict(records)
    instances = runtime_settings.instances()
    ids = set(prepared) | set(instances)
    if playground is not None:
        ids = {rid for rid in ids if runtime_settings.source(rid) == playground}
    try:
        usage = storage.usage(fresh=fresh)
    except (OSError, ValueError, compose.ComposeError) as error:
        usage = {"volumes": {}, "shared": []}
        problems.append({"file": "Docker storage", "message": str(error)})
    shared = storage.resources(compose.project_name()) if playground is None else {"containers": [], "volumes": []}
    rows = [{"id": None, "name": "Shared platform", "mode": "shared", "prepared": True,
             "containers": shared["containers"], "volumes": shared["volumes"], "ports": [],
             "local_bytes": None, "source": True, "problem": None}] if playground is None else []
    names = {rid: instances.get(rid, {}).get("name") or (prepared.get(rid) or {}).get("label") or rid for rid in ids}
    for pid in sorted(ids, key=lambda rid: (runtime_settings.source(rid), names[rid].casefold(), rid)):
        parent = runtime_settings.source(pid)
        unit = project["playgrounds"].get(parent)
        record = prepared.get(pid)
        value = {"resources": {"containers": [], "volumes": [], "networks": []}, "local": None, "error": None, "running": False}
        try:
            if record:
                value = runtime_storage.inventory(pid)
            elif independent.folder(pid).exists() or any(storage.resources(independent.name(pid)).values()):
                value["error"] = "An incomplete runtime has private resources; cleanup is held"
        except (OSError, ValueError, compose.ComposeError) as error:
            value["error"] = str(error)
        damaged = next((problem["message"] for problem in problems if problem["file"] ==
                        (independent.folder(pid) / "runtime.json").relative_to(paths.ROOT).as_posix()), None)
        if damaged:
            value["error"] = damaged
        resources = value["resources"]
        row = {"id": pid, "name": names[pid],
               "playground": parent, "playground_name": (unit or {}).get("name") or parent,
               "mode": "independent", "selected": True, "prepared": bool(record), "source": unit is not None,
               "containers": resources["containers"], "volumes": resources["volumes"],
               "local_bytes": value["local"]["bytes"] if value["local"] else None,
               "folder": independent.folder(pid).relative_to(paths.ROOT).as_posix() if record else None,
               "problem": value["error"] or ("This clone is incomplete; delete it and create a new runtime" if (record or {}).get("clone_pending") else None),
               "versions": ((record or {}).get("tested") or {}).get("versions", {}),
               "ports": sorted({str(port) for spec in (record or {}).get("services", {}).values() for port in spec.get("ports", [])}),
               "prepared_at": (record or {}).get("prepared_at"),
               "active": bool((record or {}).get("active"))}
        reason = value["error"] or ("Stop this runtime before cleanup" if value["running"] else None)
        row["cleanup"] = {"containers": {"enabled": bool(record and not reason and (resources["containers"] or resources["networks"])),
                                        "reason": reason or (None if resources["containers"] or resources["networks"] else "Containers already removed")},
                          "data": {"enabled": bool(record and not reason), "reason": reason or (None if record else "Not started yet")},
                          "delete": {"enabled": pid in instances and not reason, "reason": reason or (None if pid in instances else "Unknown runtime definition")}}
        rows.append(row)
    running = [c["name"] for row in rows for c in row["containers"] if c["state"] == "running"]
    try:
        samples = storage.load(running)
    except (OSError, ValueError, compose.ComposeError) as error:
        samples = {}
        problems.append({"file": "Docker stats", "message": str(error)})
    for row in rows:
        if row["id"] is None:
            row["ports"] = sorted({port for c in row["containers"] for port in c["ports"]})
        row["running"] = sum(c["state"] in runtime_storage.RUNNING for c in row["containers"])
        row["state"] = "Running" if row["running"] else "Stopped" if row["prepared"] else "Not started"
        row["volume_bytes"] = sum(usage["volumes"][v["name"]] for v in row["volumes"]) if not row["problem"] and all(
            usage["volumes"].get(v["name"]) is not None for v in row["volumes"]) else None
        each = [samples.get(c["name"]) for c in row["containers"] if c["state"] == "running"]
        row["cpu_percent"] = round(sum(s["cpu_percent"] for s in each), 1) if all(s is not None for s in each) else None
        row["memory_bytes"] = sum(s["memory_bytes"] for s in each) if all(s is not None for s in each) else None
        row["volumes"] = [{"name": v["name"], "bytes": usage["volumes"].get(v["name"])} for v in row["volumes"]]
        row["containers"] = [{key: c[key] for key in ("name", "service", "state", "ports")} for c in row["containers"]]
    return {"default": "shared", "runtimes": rows, "shared_storage": usage["shared"] if playground is None else [], "problems": problems}


def cleanup(pid, body):
    operation = body.get("operation")
    if body.get("check") is True:
        return runtime_storage.preview(pid, operation)
    return runtime_storage.cleanup(pid, operation, body.get("fingerprint"))


def managed_action(project, pid, operation):
    # Source can disappear after an update or an external edit. Its private runtime must still be stoppable.
    if operation == "stop" and independent.read(pid):
        return independent.stop(pid)
    return action(project, pid, operation)
