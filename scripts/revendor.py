#!/usr/bin/env python3
"""revendor.py — push the canonical vendored files out to the consumer repos.

Reads the same manifest as the four drift guards (vendor_manifest.py), so the bot and
the guards cannot disagree. Two modes:

  local (default) — copy canonical files over the sibling checkouts in the workspace
      that are present; report what changed. `--check` only reports (no writes).
      Complements the guards: the guard says "drifted", this fixes it.

  --pr — CI mode (`.github/workflows/revendor.yml`): clone each consumer repo into
      --workspace, copy, and open one `chore: re-vendor from agentix @<sha>` PR per
      repo that changed. Each consumer's own CI then validates the bump; a Contract B
      change becomes 1 human PR (agentix) + N approve-only bot PRs
      (Design B, Ludo-Odoo-Migrations/ludo-agent#558). Requires `gh` authenticated with a PAT
      that can push to the consumer repos (WORKSPACE_PAT).

Run from `agentix/` after the generators (gen_shared/gen_ts/gen_swift) are fresh —
check_shared_drift.py enforces freshness separately.
"""

from __future__ import annotations

import argparse
import filecmp
import shutil
import subprocess
import sys
from pathlib import Path

from vendor_manifest import REPO_ROOT, WORKSPACE, all_vendored_files

# Consumers all live in the Ludo-Odoo-Migrations org today; per-repo overrides in
# OWNER_BY_REPO so a consumer moving org (or a future Agentix-Kernel consumer) is
# a one-line change, not a constant hunt.
DEFAULT_OWNER = "Ludo-Odoo-Migrations"
OWNER_BY_REPO: dict[str, str] = {}
# master, not main: ludo-tests convention documented in docs/cluster; everything else main.
DEFAULT_BRANCH = {"ludo-tests": "master"}


def _sync(workspace: Path, write: bool, missing_repo_note: str) -> dict[str, list[str]]:
    """Copy drifted/missing vendored files; return {repo: [changed workspace-rel paths]}."""
    changed: dict[str, list[str]] = {}
    for canon, vendored, repo in all_vendored_files(workspace):
        if not canon.exists():
            print(f"[FAIL] missing canonical: {canon}", file=sys.stderr)
            sys.exit(1)
        if not (workspace / repo).exists():
            print(f"[skip] {missing_repo_note}: {repo}")
            continue
        if vendored.exists() and filecmp.cmp(canon, vendored, shallow=False):
            continue
        state = "update" if vendored.exists() else "create"
        rel = str(vendored.relative_to(workspace / repo))
        changed.setdefault(repo, []).append(rel)
        print(f"[{state}] {repo}/{rel}")
        if write:
            vendored.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(canon, vendored)
    return changed


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _pr_mode(workspace: Path, sha: str) -> None:
    """Clone consumers, re-vendor, open one PR per changed repo."""
    workspace.mkdir(parents=True, exist_ok=True)
    repos = sorted({repo for _, _, repo in all_vendored_files(workspace)})
    for repo in repos:
        if not (workspace / repo).exists():
            owner = OWNER_BY_REPO.get(repo, DEFAULT_OWNER)
            _run(["gh", "repo", "clone", f"{owner}/{repo}", repo, "--", "--depth", "1"], cwd=workspace)
    changed = _sync(workspace, write=True, missing_repo_note="clone failed / repo absent")
    branch = f"revendor/{sha[:12]}"
    for repo, files in changed.items():
        cwd = workspace / repo
        base = DEFAULT_BRANCH.get(repo, "main")
        _run(["git", "checkout", "-b", branch], cwd=cwd)
        _run(["git", "add", "--", *files], cwd=cwd)
        _run(
            [
                "git",
                "-c",
                "user.name=agentix-revendor",
                "-c",
                "user.email=bot@euroblaze.de",
                "commit",
                "-m",
                f"chore: re-vendor from agentix @{sha[:12]}",
            ],
            cwd=cwd,
        )
        _run(["git", "push", "-u", "origin", branch], cwd=cwd)
        _run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base,
                # Fresh shallow clones confuse gh's upstream detection
                # ("you must first push the current branch") — name it.
                "--head",
                branch,
                "--title",
                f"chore: re-vendor from agentix @{sha[:12]}",
                "--body",
                f"Automated re-vendor of canonical files from myAgentix/agentix commit {sha}.\n"
                f"Files:\n"
                + "\n".join(f"- {f}" for f in files)
                + "\n\nGuards: check_shared/internal/config/contract_drift.py (hub). "
                "Approve-and-merge once this repo's CI is green.",
            ],
            cwd=cwd,
        )
        print(f"[pr] {repo}: {len(files)} file(s) on {branch}")
    if not changed:
        print("[revendor] all consumers already in sync")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    ap.add_argument("--pr", action="store_true", help="CI mode: clone consumers + open PRs")
    ap.add_argument(
        "--workspace", type=Path, default=WORKSPACE, help="workspace root holding the consumer checkouts/clones"
    )
    ap.add_argument("--sha", default="", help="agentix commit sha for PR branch/title (--pr)")
    args = ap.parse_args()

    if args.pr:
        sha = (
            args.sha
            or subprocess.run(
                ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, check=True, capture_output=True, text=True
            ).stdout.strip()
        )
        _pr_mode(args.workspace, sha)
        return 0

    changed = _sync(args.workspace, write=not args.check, missing_repo_note="not checked out")
    total = sum(len(v) for v in changed.values())
    verb = "would change" if args.check else "changed"
    print(f"[revendor] {verb} {total} file(s) across {len(changed)} repo(s)")
    return 1 if (args.check and changed) else 0


if __name__ == "__main__":
    sys.exit(main())
