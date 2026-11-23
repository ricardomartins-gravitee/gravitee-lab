"""Merge an upstream release into a review branch without checking out or running upstream code."""

import json
import os
import re
import subprocess
from pathlib import Path

VERSION = re.compile(r"v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
REPOSITORY = re.compile(r"[\w.-]+/[\w.-]+", re.ASCII)
BASE = "refs/remotes/origin/main"
UPSTREAM = "refs/remotes/sync-upstream/main"
TAG = "refs/sync-upstream/release"


def run(*command, check=True, env=None):
    result = subprocess.run(command, capture_output=True, text=True, timeout=180,
                            stdin=subprocess.DEVNULL, env=env)
    if check and result.returncode:
        raise ValueError(result.stderr.strip() or f"{command[0]} exited with {result.returncode}")
    return result if not check else result.stdout.strip()


def api(path, *args):
    return json.loads(run("gh", "api", path, *args))


def newest_release(pages):
    candidates = []
    for page in pages:
        for release in page:
            tag = release["tag_name"]
            version = VERSION.fullmatch(tag)
            if version and not (release["draft"] or release["prerelease"]):
                candidates.append((tuple(map(int, version.groups())), tag))
    if not candidates:
        raise ValueError("Upstream has no published stable vX.Y.Z release")
    return max(candidates)[1]


def ancestor(before, after):
    result = run("git", "merge-base", "--is-ancestor", before, after, check=False)
    if result.returncode > 1:
        raise ValueError(result.stderr.strip() or "Could not compare Git history")
    return result.returncode == 0


def report(message):
    print(message)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as out:
            out.write(message + "\n")


def merge_release(base, release, tag):
    result = run("git", "merge-tree", "--write-tree", "--name-only", "--no-messages", base, release, check=False)
    if result.returncode == 1:
        files = result.stdout.splitlines()[1:]
        report(f"{tag} needs a manual merge. Conflicting paths:\n\n"
               + "\n".join(f"- `{name}`" for name in files if name))
        raise ValueError("Resolve the upstream merge on a review branch")
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git could not build the update")
    tree = result.stdout.strip()
    identity = "github-actions[bot]"
    email = "41898282+github-actions[bot]@users.noreply.github.com"
    env = {**os.environ, "GIT_AUTHOR_NAME": identity, "GIT_COMMITTER_NAME": identity,
           "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_EMAIL": email}
    return run("git", "commit-tree", tree, "-p", base, "-p", release,
               "-m", f"Merge upstream release {tag}", env=env)


def sync(repository, preview):
    repo = api(f"repos/{repository}")
    upstream = (repo.get("parent") or {}).get("full_name", "")
    if not REPOSITORY.fullmatch(upstream) or ".." in upstream or repo["default_branch"] != "main":
        raise ValueError("This workflow expects a GitHub fork with main as its default branch")
    tag = newest_release(api(f"repos/{upstream}/releases?per_page=100", "--paginate", "--slurp"))
    run("git", "fetch", "--no-tags", "origin", f"+refs/heads/main:{BASE}")
    run("git", "fetch", "--no-tags", f"https://github.com/{upstream}.git",
        f"+refs/heads/main:{UPSTREAM}", f"+refs/tags/{tag}:{TAG}")
    base = run("git", "rev-parse", BASE)
    release = run("git", "rev-parse", f"{TAG}^{{commit}}")
    if not ancestor(release, UPSTREAM):
        raise ValueError(f"Published tag {tag} is not in upstream main")
    if ancestor(release, base):
        report(f"Fork main already contains {tag}.")
        return

    branch = f"sync/upstream-{tag}"
    ref = f"refs/heads/{branch}"
    pull_requests = api(f"repos/{repository}/pulls", "-X", "GET", "-f", "state=all", "-f", "base=main",
                        "-f", f"head={repo['owner']['login']}:{branch}")
    if pull_requests:
        report(f"{tag} already has a pull request: {pull_requests[0]['html_url']}")
        return
    existing = run("git", "ls-remote", "--heads", "origin", ref)
    if existing:
        run("git", "fetch", "--no-tags", "origin", ref)
        candidate = run("git", "rev-parse", "FETCH_HEAD")
        if not ancestor(release, candidate) or not ancestor(base, candidate):
            raise ValueError(f"{branch} already exists and needs review against current main and {tag}")
    else:
        candidate = merge_release(base, release, tag)

    if preview:
        report(f"{tag} merges cleanly into fork main at {base}. Preview commit: {candidate}.")
        return
    if run("git", "ls-remote", "origin", "refs/heads/main").split()[0] != base:
        raise ValueError("Fork main moved while preparing the update. Run again on the new tip.")
    if not existing:
        run("git", "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", "--no-follow-tags", f"--force-with-lease={ref}:", "origin", f"{candidate}:{ref}")
    body = (
        f"Merge the published upstream release [{tag}](https://github.com/{upstream}/releases/tag/{tag}) "
        "into the fork. The merge retains fork main and the upstream release in its history.\n\n"
        f"- Fork main: `{base}`\n- Upstream release: `{release}`\n\n"
        "Review the changes and CI, then **create a merge commit** to preserve both histories. "
        "Branches with unreleased work stay separate. Fork releases go through the Fork release workflow."
    )
    print(run("gh", "pr", "create", "--repo", repository, "--base", "main", "--head", branch,
              "--title", f"Sync upstream {tag}", "--body", body))


if __name__ == "__main__":
    try:
        sync(os.environ["GITHUB_REPOSITORY"], os.environ.get("FORK_SYNC_PREVIEW") == "true")
    except (KeyError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(f"Fork upstream sync: {error}") from error
