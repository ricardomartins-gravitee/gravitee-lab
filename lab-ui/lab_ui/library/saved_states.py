"""Immutable, playground-only management API snapshots and their local ownership records.

Shared databases, core settings, catalogs and add-on data are never archived or replaced.
The seeder owns product-specific adapters and refuses resources it cannot restore in isolation.
Legacy whole-Lab archives stay on disk but cannot be listed or restored through this API.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .. import paths
from ..docker import compose
from ..docker.jobs import JOBS, JobFailed
from ..gravitee import places

ROOT = paths.ROOT
STATES = ROOT / ".lab/saved-states"
SID = re.compile(r"[a-f0-9]{32}")
SCHEMA = 2
MAX_MANIFEST = 1 << 20


def _home(pid):
    return STATES / hashlib.sha256(pid.encode("utf-8")).hexdigest()


def _location(pid, sid):
    if not isinstance(sid, str) or not SID.fullmatch(sid):
        raise ValueError("Invalid saved state")
    home = _home(pid)
    target = home / sid
    if any(path.is_symlink() for path in (ROOT / ".lab", STATES, home, target)):
        raise ValueError("Saved state storage must not be linked")
    return target


def _read(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MANIFEST:
        raise ValueError("The saved state manifest is not a regular file")
    obj = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(obj, dict) or type(obj.get("schema")) is not int or obj["schema"] != SCHEMA
            or obj.get("scope") != "playground"):
        raise ValueError("This legacy checkpoint contains the whole Lab and cannot restore a single playground")
    if (obj.get("complete") is not True
            or not all(isinstance(obj.get(key), str) and obj[key] for key in ("id", "playground", "name", "created"))
            or type(obj.get("size")) is not int or obj["size"] < 0):
        raise ValueError("Invalid or incomplete saved state metadata")
    try:
        datetime.fromisoformat(obj["created"].replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("Invalid saved state creation time") from None
    return obj


def list_states(pid):
    home = _home(pid)
    if (ROOT / ".lab").is_symlink() or STATES.is_symlink() or not home.is_dir() or home.is_symlink():
        return []
    result = []
    for path in home.iterdir():
        if path.is_symlink() or not path.is_dir() or not SID.fullmatch(path.name):
            continue
        try:
            data = _read(path / "manifest.json")
            if data["playground"] == pid and data["id"] == path.name:
                result.append({key: data[key] for key in ("id", "name", "created", "size")})
        except (ValueError, OSError, RecursionError):
            continue
    return sorted(result, key=lambda row: row["created"], reverse=True)


def _playground(project, pid):
    if pid not in project["playgrounds"]:
        raise paths.NotFound(f"No such playground: {pid}")
    if places.recorded(pid):
        raise ValueError("Saved states are only supported on This machine")
    playground = project["playgrounds"][pid]
    if not playground.get("seed"):
        raise ValueError("This playground has no seed ownership record")
    return playground


def delete_state(project, pid, sid):
    if pid not in project["playgrounds"]:
        raise paths.NotFound(f"No such playground: {pid}")
    target = _location(pid, sid)
    meta = _read(target / "manifest.json")
    if meta["playground"] != pid or meta["id"] != sid:
        raise ValueError("The saved state belongs to another playground")
    with JOBS.idle():
        shutil.rmtree(target)
    return {"deleted": sid, "image_cleanup_pending": False}


def _digest(file):
    digest = hashlib.sha256()
    with file.open("rb") as source:
        for chunk in iter(lambda: source.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(pid):
    """Bound only to this item's source and target settings, never its catalog's other items."""
    folder = ROOT / paths.unit_folder("playground", pid)
    actual = folder.resolve(strict=True)
    entries = []

    def visit(file, relative):
        if file.is_symlink():
            raise ValueError("A playground's checkpoint inputs must not contain file links: " + relative)
        if file.is_dir():
            for child in sorted(file.iterdir()):
                visit(child, relative + "/" + child.name)
        elif file.is_file():
            entries.append([relative, _digest(file)])
        else:
            raise ValueError("A playground checkpoint input is not a regular file: " + relative)

    for name in ("compose.yml", "seed", ".env", "secrets"):
        if (actual / name).exists():
            visit(actual / name, name)
    state = actual / ".state"
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise ValueError("The playground's state folder must not be linked")
    ownership = state / "created.json"
    if ownership.is_symlink():
        raise ValueError("The playground's ownership record must not be linked")
    return {"folder": str(actual), "inputs": hashlib.sha256(json.dumps(entries).encode()).hexdigest()}


def _write(file, data):
    paths.write_atomic(file, json.dumps(data, indent=2) + "\n", mode=0o600)


def _foreign(project, pid):
    records = []
    for other in project["playgrounds"]:
        if other == pid:
            continue
        text = places.read_state(other, places.RECORD)
        if text:
            rows = json.loads(text)
            if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
                raise ValueError("Another playground has an unreadable ownership record")
            records.extend(rows)
    return records


def _command(project, pid, operation, folder):
    playground = _playground(project, pid)
    return [*compose.COMPOSE, *compose.all_files([playground["file"]]), "run", "-d", "--rm", "--no-deps", "--pull=missing",
            "--entrypoint", "python", "-e", "PYTHONPATH=/checkpoint-code",
            "-e", f"LAB_SAVED_STATE_OWNER={os.getuid()}:{os.getgid()}",
            "-v", f"{ROOT / 'seeder'}:/checkpoint-code:ro", "-v", f"{folder}:/checkpoint",
            playground["seed"], "-m", "lab_seeder.checkpoints", operation, "/checkpoint/resources.json"]


def _execute(job, project, pid, operation, folder):
    # API restoration rolls back inside the same process. Ending its Compose client halfway
    # would leave an unobserved container writing data, so let that transaction finish.
    name = "lab-state-" + uuid.uuid4().hex
    command = _command(project, pid, operation, folder)
    position = command.index("--entrypoint")
    command[position:position] = ["--name", name]
    marker = folder / "in-flight.json"
    _write(marker, {"container": name, "operation": operation})
    rc, out, err = compose.run(command, timeout=120, env={"COMPOSE_IGNORE_ORPHANS": "true"})
    if rc:
        # The CLI can lose its answer after Docker started the transaction. Keep its files.
        raise JobFailed("Could not launch the playground saved-state adapter; transaction files retained")
    result = folder / "result.json"
    deadline = time.monotonic() + 600
    missing = 0
    while time.monotonic() < deadline:
        if result.is_file() and not result.is_symlink():
            answer = json.loads(result.read_text(encoding="utf-8"))
            marker.unlink()
            if answer.get("ok") is not True:
                raise JobFailed(answer.get("error") or "The playground saved-state operation failed")
            job.lines.append("Verified playground resources" if operation == "check" else "Finished playground " + operation)
            return
        rc, out, err = compose.run(["docker", "inspect", "--format", "{{.State.Running}}", name], timeout=10)
        missing = missing + 1 if rc and "no such object" in err.lower() else 0
        if (rc == 0 and out.strip() == "false") or missing >= 3:
            marker.unlink()
            raise JobFailed("The saved-state adapter stopped without a result")
        time.sleep(.5)
    raise JobFailed("The saved-state adapter has not finished; transaction files retained")


def capture(job, project, pid, name=None):
    _playground(project, pid)
    binding = _binding(pid)
    job.step("Save state")
    sid = uuid.uuid4().hex
    final = _location(pid, sid)
    final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(final.parent, 0o700)
    staged = Path(tempfile.mkdtemp(prefix=".pending-", dir=str(final.parent)))
    try:
        _write(staged / "foreign.json", _foreign(project, pid))
        _execute(job, project, pid, "save", staged)
        if binding != _binding(pid):
            raise JobFailed("The playground's source or links changed during the save; nothing was deleted")
        file = staged / "resources.json"
        if file.is_symlink() or not file.is_file():
            raise JobFailed("The resource adapter produced no saved state")
        data = json.loads(file.read_text(encoding="utf-8"))
        if data.get("scope") != "playground":
            raise JobFailed("The resource adapter did not produce a playground-only saved state")
        now = datetime.now(timezone.utc).isoformat()
        meta = {"schema": SCHEMA, "scope": "playground", "complete": True, "id": sid, "playground": pid,
                "name": name or "Saved " + now[:19].replace("T", " "), "created": now,
                "binding": binding, "sha256": _digest(file), "size": file.stat().st_size}
        (staged / "foreign.json").unlink()
        (staged / "result.json").unlink(missing_ok=True)
        _write(staged / "manifest.json", meta)
        staged.rename(final)
        job.lines.append("Saved playground state: " + meta["name"])
        return sid
    finally:
        if staged.exists() and not (staged / "in-flight.json").exists():
            shutil.rmtree(staged)
        elif staged.exists():
            job.lines.append("Saved-state transaction files retained at " + str(staged))


def _verified(pid, sid):
    folder = _location(pid, sid)
    data = _read(folder / "manifest.json")
    if data["id"] != sid or data["playground"] != pid:
        raise ValueError("The saved state belongs to another playground")
    if data.get("binding") != _binding(pid):
        raise ValueError("The playground's source or links changed since this state was saved")
    file = folder / "resources.json"
    if file.is_symlink() or not file.is_file() or _digest(file) != data.get("sha256"):
        raise ValueError("Saved state integrity check failed")
    return folder, data


def restore(project, pid, sid, confirmed=False, before=()):
    _playground(project, pid)
    if not confirmed:
        raise ValueError("Confirm replacing this playground's recorded resources before restoring")
    folder, data = _verified(pid, sid)

    def check(job):
        source, _ = _verified(pid, sid)
        pending = Path(tempfile.mkdtemp(prefix=".check-", dir=str(STATES)))
        try:
            shutil.copyfile(source / "resources.json", pending / "resources.json")
            os.chmod(pending / "resources.json", 0o600)
            _write(pending / "foreign.json", _foreign(project, pid))
            _execute(job, project, pid, "check", pending)
        finally:
            if not (pending / "in-flight.json").exists():
                shutil.rmtree(pending)

    def action(job):
        source, _ = _verified(pid, sid)
        job.step("Restore state")
        pending = Path(tempfile.mkdtemp(prefix=".restore-", dir=str(STATES)))
        try:
            shutil.copyfile(source / "resources.json", pending / "resources.json")
            os.chmod(pending / "resources.json", 0o600)
            _write(pending / "foreign.json", _foreign(project, pid))
            _execute(job, project, pid, "restore", pending)
        finally:
            if (pending / "previous.json").exists() or (pending / "in-flight.json").exists():
                job.lines.append("Previous playground resources retained for recovery at " + str(pending))
            else:
                shutil.rmtree(pending)
    return JOBS.start("Restore " + data["name"], [check, *before, action], key=f"playground:{pid}",
                      verb="restore", phases=["Restore state"], waits=[None])
