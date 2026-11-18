"""Plan and publish a fork release without changing upstream's version numbering.

The workflow fetches upstream tags into a separate namespace, so a fork tag cannot stand in for an upstream release.
Planning is read only. Publishing creates an immutable annotated tag and its GitHub release for the reviewed main commit.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

TAG = re.compile(r"v(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})(?:-rev\.([1-9][0-9]*))?")
COMMIT = re.compile(r"[0-9a-f]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
UPSTREAM_TAGS = "refs/release-upstream/tags/"
UPSTREAM_MAIN = "refs/remotes/release-upstream/main"
MAIN = "refs/remotes/origin/main"


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60,
                            stdin=subprocess.DEVNULL)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git could not read the release history")
    return result.stdout.strip()


def ancestor(root, before, after):
    result = subprocess.run(["git", "-C", str(root), "merge-base", "--is-ancestor", before, after],
                            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if result.returncode not in (0, 1):
        raise ValueError("Git could not compare the release history")
    return result.returncode == 0


def numbers(tag):
    found = TAG.fullmatch(tag)
    return tuple(int(part or 0) for part in found.groups()) if found else None


def refs(root, prefix, merged):
    listed = git(root, "for-each-ref", "--merged", merged, "--format=%(refname)", prefix)
    found = {}
    for ref in listed.splitlines():
        name = ref[len(prefix):]
        if numbers(name) is not None:
            found[name] = git(root, "rev-parse", f"{ref}^{{commit}}")
    return found


def plan(root, upstream, expected=""):
    if not REPOSITORY.fullmatch(upstream) or ".." in upstream:
        raise ValueError("The upstream repository must be OWNER/REPO")
    head = git(root, "rev-parse", "HEAD")
    if head != git(root, "rev-parse", MAIN):
        raise ValueError("The selected commit is not the current main; preview main again")
    if expected and (not COMMIT.fullmatch(expected) or expected != head):
        raise ValueError("The reviewed commit does not match main; preview main again")
    upstream_tags = {tag: sha for tag, sha in refs(root, UPSTREAM_TAGS, UPSTREAM_MAIN).items() if numbers(tag)[3] == 0}
    held = [tag for tag, sha in upstream_tags.items() if ancestor(root, sha, head)]
    if not held:
        raise ValueError("Main contains no upstream release; fetch upstream main and its release tags")
    parent = max(held, key=numbers)
    own = refs(root, "refs/tags/", MAIN)
    for tag, sha in own.items():
        base = tag.split("-rev.")[0]
        if base not in upstream_tags or not ancestor(root, upstream_tags[base], sha):
            raise ValueError(f"{tag} does not descend from its upstream release")
        if numbers(tag)[3] == 0 and sha != upstream_tags[base]:
            raise ValueError(f"{tag} is not the upstream release commit; a fork change needs -rev.N")
        if numbers(tag)[3] and sha == upstream_tags[base]:
            raise ValueError(f"{tag} has no fork changes; the upstream release uses its bare tag")
    if head == upstream_tags[parent]:
        tag = parent
    else:
        already = [tag for tag, sha in own.items() if sha == head and tag.startswith(parent + "-rev.")]
        # A revision number is never reused, even when someone made its tag on another branch
        named = git(root, "tag", "--list", parent + "-rev.*").splitlines()
        revisions = [numbers(tag)[3] for tag in named if numbers(tag) is not None]
        tag = max(already, key=numbers) if already else f"{parent}-rev.{max(revisions, default=0) + 1}"
    previous = max((tag for tag, sha in own.items() if sha != head), key=numbers, default="")
    start = own[previous] if previous else upstream_tags[parent]
    changes = git(root, "log", "--first-parent", "--no-merges", "--max-count=50", "--format=%s", f"{start}..{head}")
    notes = f"Based on [{parent}](https://github.com/{upstream}/releases/tag/{parent}).\n"
    notes += f"\nCommit: `{head}`\n"
    if changes:
        notes += "\n" + "\n".join(f"- {line}" for line in changes.splitlines()) + "\n"
    return {"tag": tag, "commit": head, "parent": parent, "upstream": upstream, "previous": previous, "notes": notes}


def api(repository, path, data=None, pages=False):
    command = ["gh", "api", "--method", "GET" if data is None else "POST", f"repos/{repository}/{path}"]
    if pages:
        command.extend(["--paginate", "--slurp"])
    if data is not None:
        command.extend(["--input", "-"])
    result = subprocess.run(command, input=json.dumps(data) if data is not None else None,
                            capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "GitHub could not complete the release request")
    return json.loads(result.stdout)


def publish(proposed, repository, call=api):
    tag, head = proposed["tag"], proposed["commit"]
    if not REPOSITORY.fullmatch(repository) or ".." in repository or numbers(tag) is None or not COMMIT.fullmatch(head):
        raise ValueError("The release plan is not valid")
    current = call(repository, "git/ref/heads/main")["object"]["sha"]
    if current != head:
        raise ValueError("Main moved after the preview; preview the new commit before publishing")
    matches = call(repository, f"git/matching-refs/tags/{tag}")
    existing = next((ref for ref in matches if ref["ref"] == f"refs/tags/{tag}"), None)
    if existing:
        target = existing["object"]
        for _ in range(10):
            if target["type"] == "commit":
                break
            if target["type"] != "tag":
                raise ValueError(f"{tag} does not name a commit")
            target = call(repository, f"git/tags/{target['sha']}")["object"]
        if target["type"] != "commit" or target["sha"] != head:
            raise ValueError(f"{tag} already names another commit; tags are never moved")
    else:
        annotated = call(repository, "git/tags", {"tag": tag, "message": f"Gravitee Lab {tag[1:]}",
                                                  "object": head, "type": "commit"})
        call(repository, "git/refs", {"ref": f"refs/tags/{tag}", "sha": annotated["sha"]})
    releases = call(repository, "releases?per_page=100", pages=True)
    released = next((release for page in releases for release in page if release["tag_name"] == tag), None)
    if released:
        if released["draft"]:
            raise ValueError(f"{tag} has an existing draft release; review and publish that draft on GitHub")
        return released["html_url"]
    release = call(repository, "releases", {"tag_name": tag, "target_commitish": head, "name": f"Gravitee Lab {tag[1:]}",
                                            "body": proposed["notes"], "draft": False, "prerelease": False,
                                            "make_latest": "true"})
    return release["html_url"]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("action", choices=("plan", "publish"))
    args = parser.parse_args()
    try:
        if args.action == "publish":
            print(publish(json.loads(os.environ["RELEASE_PLAN"]), os.environ["GITHUB_REPOSITORY"]))
            return
        expected = os.environ.get("EXPECTED_COMMIT", "")
        if os.environ.get("PUBLISH") == "true" and not expected:
            raise ValueError("Publishing needs the full commit SHA from the preview")
        proposed = plan(Path.cwd(), os.environ["UPSTREAM_REPOSITORY"], expected)
        encoded = json.dumps(proposed)
        print(encoded)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as handle:
                handle.write(f"plan={encoded}\n")
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                handle.write(f"## Fork release {proposed['tag']}\n\n{proposed['notes']}")
                handle.write("\nPublishing requires Publish and this exact commit SHA.\n")
    except (KeyError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"Fork release: {error}\n")


if __name__ == "__main__":
    main()
