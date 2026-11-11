"""Checkpoint adapter for registered folder catalogs, including links into their worktrees.

Only machine files are restored through these links. Source files and Git remain the person's work:
their digest and link destinations must still match before a checkpoint may replace any runtime data.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tarfile
from pathlib import Path

from ..catalogs import kept, registry

RECORD = "linked-catalogs.json"
READONLY_RECORD = "readonly-inputs.json"
PAYLOAD = "linked-state"
SKIP = {".git", ".worktrees", "__pycache__"}
LIMIT = 1 << 20


def source_path(value):
    """Docker Desktop can report a bind by its VM path after the container writes to it."""
    path = Path(value)
    if sys.platform == "darwin" and str(path).startswith("/host_mnt/") and not path.exists():
        host = Path(str(path)[len("/host_mnt"):])
        if host.exists():
            path = host
    return path.resolve()


def _digest(file):
    value = hashlib.sha256()
    with file.open("rb") as stream:
        for data in iter(lambda: stream.read(1 << 20), b""):
            value.update(data)
    return value.hexdigest()


def _machine(path):
    """Machine files of the catalog itself or of one of its items, never source folders."""
    parts = Path(path).parts
    return bool(parts and (parts[0] in kept.MACHINE_FILES or
                          len(parts) >= 3 and parts[0] in ("addons", "playgrounds") and parts[2] in kept.MACHINE_FILES))


def _plain_tree(path):
    if path.is_symlink():
        raise ValueError("A linked catalog's machine files must not contain links")
    if path.is_dir():
        for child in path.iterdir():
            _plain_tree(child)
    elif path.exists() and not path.is_file():
        raise ValueError("A linked catalog's machine files must be regular files or directories")


def read(root):
    """Inventory only the folder catalogs the user registered, with bounded, acyclic internal links."""
    base = root / ".lab/catalogs"
    if not base.exists():
        return []
    if base.is_symlink():
        raise ValueError("The catalogs directory must not be linked")
    configured = {one["id"]: one for one in (registry.read_config() or {}).get("catalogs", [])}
    result = []
    for folder in sorted(base.iterdir()):
        if not folder.is_symlink():
            continue
        entry = configured.get(folder.name, {})
        target = folder.resolve(strict=True)
        if entry.get("kind") != "folder" or Path(entry.get("path", "")).expanduser().resolve() != target:
            raise ValueError("Only registered folder catalogs may be saved through links: " + folder.name)
        if not target.is_dir() or target == root.resolve() or target in root.resolve().parents:
            raise ValueError("A linked catalog must have its own folder")
        source, links, directories = [], [], []

        def visit(path, relative, ancestors):
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(target):
                raise ValueError("A linked catalog path leaves its registered folder: " + folder.name + "/" + relative)
            if path.is_symlink():
                links.append([relative, os.readlink(path), str(resolved)])
            mode = path.stat().st_mode
            if _machine(relative):
                _plain_tree(path)
                return
            if stat.S_ISDIR(mode):
                if resolved in ancestors:
                    raise ValueError("A linked catalog contains a link cycle: " + folder.name)
                directories.append(relative)
                source.append([relative, "directory", stat.S_IMODE(mode)])
                for child in sorted(path.iterdir()):
                    if child.name not in SKIP:
                        visit(child, (relative + "/" if relative else "") + child.name, (*ancestors, resolved))
            elif stat.S_ISREG(mode):
                source.append([relative, "file", stat.S_IMODE(mode), _digest(path)])
            else:
                raise ValueError("Unsupported file in linked catalog: " + folder.name + "/" + relative)

        visit(folder, "", ())
        machine = sorted({(relative + "/" if relative else "") + name
                          for relative in directories if not relative or
                          len(Path(relative).parts) == 2 and Path(relative).parts[0] in ("addons", "playgrounds")
                          for name in kept.MACHINE_FILES})
        for name in machine:
            _plain_tree(folder / name)
        result.append({"id": folder.name, "link": os.readlink(folder), "target": str(target),
                       "links": links, "source": hashlib.sha256(json.dumps(source).encode()).hexdigest(),
                       "machine": machine})
    return result


def accepts(root, catalogs, mount):
    """A bind must be reachable through a catalog's logical tree; writable binds must be machine files."""
    source = source_path(mount.get("Source") or "/")
    for item in catalogs:
        folder = root / ".lab/catalogs" / item["id"]
        # Docker may report the resolved worktree path rather than the path through the catalog link.
        candidates = [(folder, ""), *((folder / link[0], link[0]) for link in item["links"] if link[0])]
        for path, relative in candidates:
            try:
                suffix = source.relative_to(path.resolve())
            except ValueError:
                continue
            logical = Path(relative) / suffix
            if any(part in SKIP for part in logical.parts):
                continue
            if (folder / logical).resolve() != source:
                continue
            if not mount.get("RW") or _machine(logical):
                return True
    return False


def _add_record(archive, name, value):
    data = json.dumps(value).encode()
    if len(data) > LIMIT:
        raise ValueError("The saved source inventory is too large")
    info = tarfile.TarInfo(name)
    info.mode, info.size = 0o600, len(data)
    archive.addfile(info, io.BytesIO(data))


def add(archive, root, catalogs, readonly=()):
    """The record is covered by the configuration archive's checksum, alongside the machine files."""
    def regular(info):
        if not (info.isfile() or info.isdir()):
            raise ValueError("A linked catalog's machine files must not contain links or special files")
        return info

    _add_record(archive, RECORD, catalogs)
    _add_record(archive, READONLY_RECORD, {name: _digest(root / name) for name in readonly})
    for index, item in enumerate(catalogs):
        folder = root / ".lab/catalogs" / item["id"]
        for name in item["machine"]:
            path = folder / name
            if path.exists():
                _plain_tree(path)
                archive.add(path, arcname=f"{PAYLOAD}/{index}/{name}", recursive=True, filter=regular)
    if read(root) != catalogs:
        raise ValueError("Linked catalog source or links changed while saving")


def _record(archive, name, default):
    records = [member for member in archive.getmembers() if member.name == name]
    if len(records) > 1 or records and (not records[0].isfile() or records[0].size > LIMIT):
        raise ValueError("Invalid saved source inventory")
    return json.load(archive.extractfile(records[0])) if records else default


def verified(archive, root):
    """Reject changed source, retargeted links and forged destinations before any data is replaced."""
    saved = _record(archive, RECORD, [])
    readonly = _record(archive, READONLY_RECORD, {})
    if not isinstance(readonly, dict):
        raise ValueError("Invalid read-only source inventory")
    for name, digest in readonly.items():
        file = root / name
        if (not name or Path(name).is_absolute() or any(part in ("..", ".git") for part in Path(name).parts)
                or file.is_symlink() or not file.resolve().is_relative_to(root.resolve()) or not file.is_file()
                or _digest(file) != digest):
            raise ValueError("A read-only runtime file changed; restore its saved version before restoring this state")
    current = read(root)
    if saved != current:
        raise ValueError("Linked catalog source or links changed; restore their saved version before restoring this state")
    return current


def member(name, catalogs):
    parts = Path(name).parts
    if name != Path(name).as_posix() or len(parts) < 3 or parts[0] != PAYLOAD or not parts[1].isdigit() or str(int(parts[1])) != parts[1]:
        return False
    index = int(parts[1])
    return index < len(catalogs) and _machine(Path(*parts[2:])) and any(
        str(Path(*parts[2:])) == path or str(Path(*parts[2:])).startswith(path + "/")
        for path in catalogs[index]["machine"])


def restore(archive, root, catalogs):
    """Restore machine files only, keeping the links, source and Git metadata in place."""
    if verified(archive, root) != catalogs:
        raise ValueError("Linked catalogs changed while restoring")
    for item in catalogs:
        folder = root / ".lab/catalogs" / item["id"]
        for name in item["machine"]:
            target = folder / name
            _plain_tree(target)
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
    directories = []
    for item in archive.getmembers():
        if not item.name.startswith(PAYLOAD + "/"):
            continue
        if not member(item.name, catalogs) or not (item.isdir() or item.isfile()):
            raise ValueError("Invalid linked catalog machine file")
        _, index, relative = item.name.split("/", 2)
        target = root / ".lab/catalogs" / catalogs[int(index)]["id"] / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if item.isdir():
            target.mkdir(exist_ok=True)
            directories.append((target, item.mode & 0o777))
        else:
            with archive.extractfile(item) as source, open(target, "wb") as dest:
                shutil.copyfileobj(source, dest)
            os.chmod(target, item.mode & 0o777)
    for target, mode in reversed(directories):
        os.chmod(target, mode)
