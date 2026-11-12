"""Machine-local playground runtime choices, independent of catalog source and linked folders.

The default applies to newly assigned playgrounds. Existing playgrounds are explicitly
kept shared on first upgrade; changing the default never moves an assigned runtime.
"""

import json
import threading

from .. import paths

FILE = ".lab/playground-runtimes.json"
MODES = ("shared", "independent")
LOCK = threading.RLock()


def read():
    file = paths.ROOT / FILE
    if not file.exists():
        return {"schema": 1, "default": "independent", "playgrounds": {}}
    try:
        value = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError("Playground runtime settings cannot be read") from error
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value.get("schema") != 1 or value.get("default") not in MODES
            or not isinstance(value.get("playgrounds"), dict)
            or any(not paths.is_id(pid) or mode not in MODES for pid, mode in value["playgrounds"].items())):
        raise ValueError("Playground runtime settings are damaged")
    return value


def _write(value):
    file = paths.ROOT / FILE
    file.parent.mkdir(parents=True, exist_ok=True)
    paths.write_atomic(file, json.dumps(value, indent=2) + "\n")


def initialize(ids):
    with LOCK:
        if not (paths.ROOT / FILE).exists():
            value = read()
            value["playgrounds"] = {pid: "shared" for pid in ids}
            _write(value)


def mode(pid):
    value = read()
    # A server that has not initialized its settings yet must never move existing data.
    return value["playgrounds"].get(pid, value["default"] if (paths.ROOT / FILE).exists() else "shared")


def save(default=None, pid=None, selected=None):
    with LOCK:
        value = read()
        if default is not None:
            if default not in MODES:
                raise ValueError("Choose Shared or Independent")
            value["default"] = default
        if pid is not None:
            if not paths.is_id(pid):
                raise paths.NotFound("No such playground")
            selected = value["default"] if selected == "default" else selected
            if selected not in MODES:
                raise ValueError("Choose Shared or Independent")
            value["playgrounds"][pid] = selected
        _write(value)
        return value


def assign(pid):
    with LOCK:
        value = read()
        if pid not in value["playgrounds"]:
            value["playgrounds"][pid] = mode(pid)
            _write(value)
        return value["playgrounds"][pid]


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
    """Catalog removal and replacement must also honor private runtime dependencies."""
    return {ns for value in _runtimes() if value["active"] for ns in value["catalogs"]}


def active_projects():
    """The private Compose projects to include when measuring this Lab's resource use."""
    return {value["project"] for value in _runtimes() if value["active"] and isinstance(value.get("project"), str)}


def holds(kind, uid):
    """A source item cannot be removed or moved while a private runtime uses it."""
    return any(value["active"] and [kind, uid] in value.get("units", []) for value in _runtimes())
