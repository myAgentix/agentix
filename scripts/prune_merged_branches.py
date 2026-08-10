#!/usr/bin/env python3
"""Prune branches whose commits are already in main.

The repo squash-merges PRs. A squash rewrites commits, so a merged branch tip
never becomes an ancestor of ``main`` and ``git branch --merged`` never lists
it. Classification here uses ``git cherry``, which compares **patch ids**: a
branch is merged when every one of its commits has an equivalent already
upstream (``git cherry`` marks those ``-``). Ancestry is never consulted.

Protected, never deleted:

* ``main`` and any branch checked out in **any** worktree (git refuses ``-D`` on
  those, and a linked worktree is someone's live working state),
* the head branch of every open PR (via ``gh``; a ``gh`` failure is fatal
  rather than silently unprotecting a live PR),
* anything passed with ``--keep``.

Default is a dry run. Pass ``--apply`` to actually delete.

    scripts/prune_merged_branches.py                    # dry run, both scopes
    scripts/prune_merged_branches.py --apply            # delete local + remote
    scripts/prune_merged_branches.py --apply --local    # local only

Deleting a squash-merged branch loses nothing: the patches are in ``main`` and
the merge commits remain. Diverged branches are reported and kept — they need a
human decision, not a sweep.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass

# Upstream everything is compared against.
UPSTREAM = "origin/main"
REMOTE = "origin"

# Refs that are never branch candidates.
_SKIP_REFS = {"HEAD", "main"}


class GitError(RuntimeError):
    """A git/gh invocation failed."""


def _run(*args: str, check: bool = True) -> str:
    """Run a command and return stdout, stripped."""
    proc = subprocess.run(args, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout.strip()


@dataclass
class Branch:
    """One branch and its classification against UPSTREAM."""

    name: str  # short name, e.g. "docs/foo"
    remote: bool  # True = origin/<name>, False = local
    ahead: int  # commits not in UPSTREAM by ancestry
    equivalent: int  # of those, how many have an upstream patch-equivalent
    protected: str | None = None  # reason, if protected

    @property
    def ref(self) -> str:
        return f"{REMOTE}/{self.name}" if self.remote else self.name

    @property
    def merged(self) -> bool:
        """Every commit already upstream (or nothing to compare)."""
        return self.ahead == self.equivalent

    @property
    def verdict(self) -> str:
        if self.protected:
            return f"KEEP ({self.protected})"
        return "DELETE" if self.merged else "KEEP (diverged)"


def worktree_branches() -> dict[str, str]:
    """Branch name -> worktree path, for every branch checked out in a worktree.

    Includes the main worktree. Git refuses ``branch -D`` on any of these, so
    they must be classified as protected rather than attempted and failed.
    """
    held: dict[str, str] = {}
    path = ""
    for line in _run("git", "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :]
        elif line.startswith("branch "):
            ref = line[len("branch ") :]
            held[ref.removeprefix("refs/heads/")] = path
    return held


def open_pr_head_refs() -> set[str]:
    """Head branch names of every open PR.

    A ``gh`` failure raises: guessing here could delete a live PR's branch.
    """
    raw = _run("gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "headRefName")
    return {pr["headRefName"] for pr in json.loads(raw or "[]")}


def classify(
    name: str,
    *,
    remote: bool,
    keep: set[str],
    protected_names: set[str],
    held: dict[str, str],
) -> Branch:
    """Build a Branch with its patch-equivalence counts filled in."""
    ref = f"{REMOTE}/{name}" if remote else name
    ahead = int(_run("git", "rev-list", "--count", f"{UPSTREAM}..{ref}") or 0)
    cherry = _run("git", "cherry", UPSTREAM, ref, check=False)
    equivalent = sum(1 for line in cherry.splitlines() if line.startswith("-"))

    reason = None
    if name in keep:
        reason = "--keep"
    elif name in protected_names:
        reason = "open PR"
    elif not remote and name in held:
        # A remote branch is deletable even while a local worktree holds the
        # same name; only the local ref is pinned by git.
        reason = "in worktree"
    return Branch(name=name, remote=remote, ahead=ahead, equivalent=equivalent, protected=reason)


def collect(*, remote: bool, keep: set[str], protected_names: set[str], held: dict[str, str]) -> list[Branch]:
    """Classify every candidate branch in one scope."""
    if remote:
        raw = _run("git", "branch", "-r", "--format=%(refname:short)")
        names = []
        for line in raw.splitlines():
            short = line.strip()
            if not short.startswith(f"{REMOTE}/"):
                continue
            name = short[len(REMOTE) + 1 :]
            # Skip HEAD, main, and any ref that isn't a real branch path.
            if name in _SKIP_REFS or not name:
                continue
            names.append(name)
    else:
        raw = _run("git", "branch", "--format=%(refname:short)")
        names = [n.strip() for n in raw.splitlines() if n.strip() and n.strip() not in _SKIP_REFS]

    return [classify(n, remote=remote, keep=keep, protected_names=protected_names, held=held) for n in names]


def report(branches: list[Branch], scope: str) -> None:
    """Print the classification table for one scope."""
    if not branches:
        print(f"\n{scope}: nothing to classify")
        return
    print(f"\n{scope} ({len(branches)} branches)")
    print(f"  {'branch':<48} {'ahead':>5} {'equiv':>5}  verdict")
    for b in sorted(branches, key=lambda x: (x.verdict.startswith("DELETE"), x.name)):
        print(f"  {b.name:<48} {b.ahead:>5} {b.equivalent:>5}  {b.verdict}")


def delete_local(names: list[str]) -> list[str]:
    """Delete each local branch. Returns the names that failed.

    Deletions are independent, so one failure must never abandon the rest of
    the run (nor the remote scope that follows).
    """
    failed: list[str] = []
    for name in names:
        try:
            # -D not -d: a squash-merged branch is not an ancestor, so -d refuses it.
            print(f"  deleted local  {name}: {_run('git', 'branch', '-D', name)}")
        except GitError as exc:
            print(f"  FAILED local   {name}: {exc}")
            failed.append(name)
    return failed


def delete_remote(names: list[str]) -> list[str]:
    """Delete remote branches in one push, falling back to per-branch on failure.

    Returns the names that failed. A single bad ref fails the whole batch push,
    so the fallback isolates it instead of losing the other 20-odd deletions.
    """
    if not names:
        return []
    try:
        _run("git", "push", REMOTE, "--delete", *names)
        for name in names:
            print(f"  deleted remote {REMOTE}/{name}")
        return []
    except GitError as exc:
        print(f"  batch push failed ({exc}) — retrying one at a time")

    failed: list[str] = []
    for name in names:
        try:
            _run("git", "push", REMOTE, "--delete", name)
            print(f"  deleted remote {REMOTE}/{name}")
        except GitError as exc:
            print(f"  FAILED remote  {name}: {exc}")
            failed.append(name)
    return failed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--local", action="store_true", help="local branches only")
    parser.add_argument("--remote", action="store_true", help="remote branches only")
    parser.add_argument("--keep", action="append", default=[], help="branch name to protect (repeatable)")
    args = parser.parse_args()

    # Neither flag = both scopes.
    do_local = args.local or not args.remote
    do_remote = args.remote or not args.local

    keep = set(args.keep)
    protected_names = open_pr_head_refs()
    held = worktree_branches()

    print(f"upstream: {UPSTREAM} @ {_run('git', 'rev-parse', '--short', UPSTREAM)}")
    print(f"protected by open PR: {sorted(protected_names) or 'none'}")
    print(f"pinned by a worktree: {sorted(held) or 'none'}")
    if keep:
        print(f"protected by --keep: {sorted(keep)}")

    doomed_local: list[str] = []
    doomed_remote: list[str] = []

    if do_local:
        locals_ = collect(remote=False, keep=keep, protected_names=protected_names, held=held)
        report(locals_, "LOCAL")
        doomed_local = [b.name for b in locals_ if b.merged and not b.protected]
    if do_remote:
        remotes = collect(remote=True, keep=keep, protected_names=protected_names, held=held)
        report(remotes, "REMOTE")
        doomed_remote = [b.name for b in remotes if b.merged and not b.protected]

    print(f"\nto delete: {len(doomed_local)} local, {len(doomed_remote)} remote")
    if not args.apply:
        print("dry run — re-run with --apply to delete")
        return 0

    print("\napplying:")
    failed = delete_local(doomed_local) + delete_remote(doomed_remote)
    if failed:
        # Loud, and a non-zero exit: a partial prune must not read as success.
        print(f"\n{len(failed)} deletion(s) FAILED: {failed}")
        return 1
    print("\ndone")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(1)
