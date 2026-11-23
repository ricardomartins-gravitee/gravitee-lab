"""Choose the published image for the same seeder sources, leaving development builds available."""

import hashlib
import json
import re
import sys
from pathlib import Path

from . import compose
from .jobs import JobFailed
from .. import paths

REPOSITORY = re.compile(r"ghcr\.io/[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*/seeder")
SEED = "io.gravitee.role"


def tag(root):
    """Hash the files the seeder Dockerfile uses, apart from files excluded by .dockerignore."""
    source = Path(root) / "seeder"
    files = [source / name for name in (".dockerignore", "Dockerfile", "requirements.txt", "seed.py")]
    package = source / "lab_seeder"
    if source.is_symlink() or package.is_symlink() or not package.is_dir():
        raise ValueError("The seeder has a missing or linked source directory")
    files.extend(path for path in package.rglob("*")
                 if "__pycache__" not in path.parts and path.suffix != ".pyc"
                 and path.relative_to(package).as_posix() not in ("seedlint.py", "testkit.py")
                 and (path.is_file() or path.is_symlink()))
    digest = hashlib.sha256()
    for path in sorted(files):
        if path.is_symlink() or not path.is_file():
            raise ValueError("The seeder has a missing or linked build input")
        digest.update(path.relative_to(source).as_posix().encode() + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return "source-" + digest.hexdigest()


def _builds_engine(service):
    """Only the ordinary build of the engine's own Dockerfile may be replaced."""
    build = service.get("build")
    return ((service.get("labels") or {}).get(SEED) == "seed"
            and isinstance(build, dict) and set(build) <= {"context", "dockerfile"}
            and build.get("dockerfile", "Dockerfile") == "Dockerfile"
            and isinstance(build.get("context"), str)
            and Path(build["context"]).resolve() == (paths.ROOT / "seeder").resolve()
            and service.get("pull_policy") in (None, "build"))


def files(job, repository, name, args=(), env=None, remove=False):
    """Compose file arguments for a seed action, with a private override when its image is available."""
    if not name or not isinstance(repository, str) or not REPOSITORY.fullmatch(repository):
        return args
    original = args or ["-f", compose.CORE_FILE, "-f", compose.ON_FILE]
    try:
        config = compose.config_json(original, timeout=15, environ={**compose.environment(), **env} if env else None)
        if not _builds_engine(config.get("services", {}).get(name, {})):
            return args
        image = repository + ":" + tag(paths.ROOT)
    except (OSError, ValueError, RuntimeError):
        return args   # Compose will report its own error if the original configuration cannot run

    code, _, _ = compose.run(["docker", "image", "inspect", "--format", "{{.Id}}", image], timeout=10)
    if code:
        if remove:   # Delete never builds or fetches a different version just to remove data.
            return args
        try:
            job.exec(["docker", "pull", image])
        except JobFailed:
            if job.canceled:
                raise
            job.lines.append("Published seeder unavailable. Using the playground's configured build.")
            return args
    text = f"services:\n  {json.dumps(name)}:\n    image: {json.dumps(image)}\n    build: !reset null\n    pull_policy: missing\n"
    override = compose.scratch() / f"seeder-{hashlib.sha256(text.encode()).hexdigest()}.yml"
    if not override.is_file():
        paths.write_atomic(override, text)
    return [*original, "-f", str(override)]


if __name__ == "__main__":
    print(tag(sys.argv[1]))
