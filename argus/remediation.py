"""Turns an approved remediation into a GitOps change. Argus never touches the cluster directly:
it edits the manifest in Git, opens a pull request, and a human merges it (Argo CD / Flux sync it).
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.request
from pathlib import Path

import yaml

from argus.agent import to_markdown

CHANGES = {"rollback_image", "increase_memory_limit", "scale_replicas"}


class _NoAliases(yaml.SafeDumper):  # keep manifests free of &id001 anchors
    def ignore_aliases(self, data):
        return True


def apply_to_manifest(result: dict, repo_dir: str) -> Path | None:
    """Edit apps/<component>/deployment.yaml in the GitOps repo. Returns the file, or None if no change."""
    rem = result["remediation"]
    if rem.get("action") not in CHANGES:
        return None
    path = Path(repo_dir) / "apps" / result["report"]["component"] / "deployment.yaml"
    docs = list(yaml.safe_load_all(path.read_text()))
    dep = next(d for d in docs if d and d.get("kind") == "Deployment")
    container = dep["spec"]["template"]["spec"]["containers"][0]
    params = rem["params"]
    if rem["action"] == "rollback_image":
        container["image"] = params["image"]
    elif rem["action"] == "increase_memory_limit":
        container.setdefault("resources", {}).setdefault("limits", {})["memory"] = params["memory"]
    else:
        dep["spec"]["replicas"] = int(params["replicas"])
    # shortcut: PyYAML drops comments in the edited file; switch to ruamel.yaml if your manifests rely on them.
    path.write_text(yaml.dump_all(docs, Dumper=_NoAliases, sort_keys=False))
    return path


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True, text=True).stdout.strip()


def propose_change(result: dict, repo_dir: str) -> dict:
    """Commit the change on a branch and open a PR when GitHub is configured. Returns what happened."""
    if os.getenv("ARGUS_GITHUB_REPO") and os.getenv("GITHUB_TOKEN"):
        _git(repo_dir, "pull", "--ff-only", "-q")  # long-running pod: edit today's main, not the one cloned at start
    path = apply_to_manifest(result, repo_dir)
    if path is None:
        return {"status": "no-change", "reason": f"action is {result['remediation'].get('action')!r}"}
    diff = _git(repo_dir, "diff")
    if not diff:
        return {"status": "no-change", "reason": "the manifest in Git already matches the proposed state"}
    base = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD")
    branch = f"argus/{result['incident_id']}"
    action, name = result["remediation"]["action"], result["report"]["component"]
    _git(repo_dir, "checkout", "-B", branch)
    try:
        _git(repo_dir, "add", str(path.resolve()))
        _git(
            repo_dir,
            "-c",
            "user.name=argus-bot",
            "-c",
            "user.email=argus-bot@users.noreply.github.com",
            "commit",
            "-m",
            f"argus: {action} for {name} ({result['incident_id']})",
        )
        repo, token = os.getenv("ARGUS_GITHUB_REPO"), os.getenv("GITHUB_TOKEN")
        if not (repo and token):
            return {
                "status": "committed",
                "branch": branch,
                "diff": diff,
                "note": "set ARGUS_GITHUB_REPO and GITHUB_TOKEN to push and open a pull request",
            }
        _git(repo_dir, "push", "--force", "-u", "origin", branch)
        body = {"title": f"[Argus] {action} for {name}", "head": branch, "base": base, "body": to_markdown(result)}
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/pulls",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return {"status": "pr-opened", "branch": branch, "url": json.load(resp)["html_url"], "diff": diff}
    finally:
        _git(repo_dir, "checkout", base)
