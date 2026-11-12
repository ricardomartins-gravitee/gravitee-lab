"""Named runtime identities beneath playgrounds; ordinary playgrounds always use Shared.

The registry is machine-local. Legacy private projects keep their Docker identities
and folders when exposed as named children; creating another instance never reassigns data.
"""

import json
import threading
import uuid

from .. import paths

FILE = ".lab/playground-runtimes.json"
LOCK = threading.RLock()


def read():
    file = paths.ROOT / FILE
    if not file.exists():
        return {"schema": 1, "default": "shared", "playgrounds": {}, "instances": {}}
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Playground runtime registry cannot be read") from error
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1
            or value.get("default") not in ("shared", "independent") or not isinstance(value.get("playgrounds"), dict)
            or any(not paths.is_id(pid) or mode not in ("shared", "independent") for pid, mode in value["playgrounds"].items())
            or not isinstance(value.get("instances", {}), dict)):
        raise ValueError("Playground runtime registry is damaged")
    identities, names = set(), set()
    for rid, row in value.get("instances", {}).items():
        if (not paths.is_id(rid) or not isinstance(row, dict) or not paths.is_id(row.get("playground"))
                or not isinstance(row.get("name"), str) or not row["name"].strip() or len(row["name"]) > 60
                or not paths.is_id(row.get("storage", rid))):
            raise ValueError("A named runtime identity is damaged")
        identity, named = row.get("storage", rid), (row["playground"], row["name"].casefold())
        if identity in identities or named in names or identity == row["playground"] and "storage" not in row:
            raise ValueError("Named runtime identities are ambiguous")
        identities.add(identity)
        names.add(named)
    return value


def _write(value):
    file = paths.ROOT / FILE
    file.parent.mkdir(parents=True, exist_ok=True)
    paths.write_atomic(file, json.dumps(value, indent=2) + "\n", mode=0o600)


def instances():
    return read().get("instances", {})


def key(rid):
    return instances().get(rid, {}).get("storage", rid)


def source(rid):
    return instances().get(rid, {}).get("playground", rid)


def alias(identity):
    return next((rid for rid, row in instances().items() if row.get("storage", rid) == identity), identity)


def mode(pid):
    return "independent" if pid in instances() else "shared"


def initialize(ids):
    """Expose legacy private data as a child without moving files, volumes, containers or ports."""
    with LOCK:
        value = read()
        before = json.dumps(value, sort_keys=True)
        rows = value.setdefault("instances", {})
        stored = {row.get("storage", rid) for rid, row in rows.items()}
        for old in _runtimes():
            identity = old.get("id")
            if paths.is_id(identity) and identity not in stored:
                rid = "runtime-" + uuid.uuid4().hex
                pid = old.get("playground", identity)
                label, index = "Independent", 1
                names = {row["name"].casefold() for row in rows.values() if row["playground"] == pid}
                while label.casefold() in names:
                    index += 1
                    label = "Independent " + str(index)
                rows[rid] = {"playground": pid, "name": label, "storage": identity}
                stored.add(identity)
        value["default"] = "shared"
        value["playgrounds"] = {pid: "shared" for pid in ids}
        if json.dumps(value, sort_keys=True) != before or not (paths.ROOT / FILE).exists():
            _write(value)


def create(pid, name):
    if not paths.is_id(pid):
        raise paths.NotFound("No such playground")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60 or any(ord(c) < 32 for c in name):
        raise ValueError("Give the runtime a name of 1 to 60 characters")
    name = name.strip()
    if name.casefold() == "shared":
        raise ValueError("Shared names the playground's normal runtime; choose another name")
    with LOCK:
        value = read()
        rows = value.setdefault("instances", {})
        if any(row["playground"] == pid and row["name"].casefold() == name.casefold() for row in rows.values()):
            raise ValueError("This playground already has a runtime with that name")
        rid = "runtime-" + uuid.uuid4().hex
        rows[rid] = {"playground": pid, "name": name}
        _write(value)
        return {"id": rid, **rows[rid]}


def forget(rid):
    with LOCK:
        value = read()
        if rid not in value.get("instances", {}):
            raise paths.NotFound("No such runtime")
        del value["instances"][rid]
        _write(value)


def _runtimes():
    found = []
    for file in (paths.ROOT / ".lab/runtimes").glob("*/runtime.json"):
        try:
            value = json.loads(file.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or type(value.get("active")) is not bool
                    or not isinstance(value.get("catalogs"), list)
                    or any(not isinstance(ns, str) for ns in value["catalogs"])):
                raise ValueError("Invalid runtime record")
            found.append(value)
        except (OSError, ValueError) as error:
            raise ValueError("An independent runtime record cannot be read; catalog changes are held") from error
    return found


def active_catalogs():
    return {ns for value in _runtimes() if value["active"] for ns in value["catalogs"]}


def active_projects():
    return {value["project"] for value in _runtimes() if value["active"] and isinstance(value.get("project"), str)}


def holds(kind, uid):
    return any(value["active"] and [kind, uid] in value.get("units", []) for value in _runtimes())
