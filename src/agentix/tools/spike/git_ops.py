"""git_status / git_diff / git_commit / git_revert — module-mode VCS tools.

 substrate. The agent commits per logical change so the operator
can review the port as a normal PR. The output dir is auto-`git init`'d
by the app's spike driver before the loop starts; the agent operates on a
``<branch_prefix><session>`` branch only.

Author identity is app-registered (:func:`register_agent_git_identity`, same
extender seam as the sandbox allowlists) so ``git log`` cleanly separates agent
commits from operator commits. Kernel defaults are neutral (``agentix-agent``).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

import structlog
from pydantic import BaseModel, Field

from agentix.tools._sandbox import output_root
from agentix.tools.base import Tool, ToolContext, elapsed_ms, ensure_input

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class AgentGitIdentity:
    """The agent's git namespace + author identity for spike VCS tools.

    Apps register their own at startup (seam — like the sandbox allowlist
    extenders); the branch prefix also guards ``git_revert`` so the agent can
    never touch operator branches.
    """

    branch_prefix: str = "agentix/port-spike-"
    name: str = "agentix-agent"
    email_domain: str = "agentix.local"


_IDENTITY = AgentGitIdentity()


def register_agent_git_identity(identity: AgentGitIdentity) -> None:
    """App seam: set the agent's git branch namespace + author identity."""
    global _IDENTITY
    _IDENTITY = identity


def agent_git_identity() -> AgentGitIdentity:
    """The registered identity (kernel-neutral defaults until an app registers)."""
    return _IDENTITY


# ─────────────────────── git_status ───────────────────────


class GitStatusInput(BaseModel):
    pass


class GitStatusOutput(BaseModel):
    branch: str
    clean: bool
    porcelain: str
    # SafetyGate verifier signal: True when the working tree changed after the write this call guards.
    # A no-op / failed write leaves the tree clean, so ok=False and SafetyGate treats it as drift.
    ok: bool = True
    latency_ms: int = 0


class GitStatus(Tool):
    name = "git_status"
    description = (
        "Show the agent's current branch + porcelain status. Doubles as the "
        "verifier for write_file / apply_patch — confirms the working tree "
        "actually changed after a write."
    )
    input_schema = GitStatusInput
    output_schema = GitStatusOutput
    mutates_target = False
    verifier: str | None = None

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        started = time.perf_counter_ns()
        cwd = output_root(ctx)
        branch_proc = await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
        status_proc = await _run_git(["status", "--porcelain"], cwd=cwd)
        dirty = bool(status_proc.stdout.strip())
        out = GitStatusOutput(
            branch=branch_proc.stdout.strip() or "(detached)",
            clean=not dirty,
            porcelain=status_proc.stdout.strip(),
            ok=dirty,
            latency_ms=elapsed_ms(started),
        )
        log.info("spike.git_status", branch=out.branch, clean=out.clean)
        return out


# ─────────────────────── git_diff ─────────────────────────


class GitDiffInput(BaseModel):
    paths: list[str] = Field(
        default_factory=list,
        description="Optional list of paths to scope the diff. Empty = full diff.",
    )
    staged: bool = Field(
        default=False,
        description="Show the staged diff (index vs HEAD) when True; else working tree vs index.",
    )


class GitDiffOutput(BaseModel):
    diff: str
    truncated: bool = False
    latency_ms: int = 0


class GitDiff(Tool):
    name = "git_diff"
    description = (
        "Show a diff of the agent's edits in the spike output dir. Use this "
        "before git_commit to see what's about to be committed."
    )
    input_schema = GitDiffInput
    output_schema = GitDiffOutput
    mutates_target = False
    verifier: str | None = None

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        params = ensure_input(input, GitDiffInput)
        started = time.perf_counter_ns()
        cwd = output_root(ctx)
        argv = ["diff"]
        if params.staged:
            argv.append("--cached")
        if params.paths:
            argv.append("--")
            argv.extend(params.paths)
        proc = await _run_git(argv, cwd=cwd)
        diff_text = proc.stdout
        truncated = False
        if len(diff_text) > 32_000:
            diff_text = diff_text[:32_000] + "\n…(truncated)"
            truncated = True
        out = GitDiffOutput(
            diff=diff_text,
            truncated=truncated,
            latency_ms=elapsed_ms(started),
        )
        log.info("spike.git_diff", bytes=len(proc.stdout), truncated=truncated)
        return out


# ─────────────────────── git_commit ───────────────────────


class GitCommitInput(BaseModel):
    message: str = Field(
        ...,
        description=(
            "Commit message. Per logical change (manifest bump → models → "
            "views → assets) so the operator can review as a normal PR."
        ),
    )
    paths: list[str] = Field(
        default_factory=list,
        description=(
            "Files to include. Empty = stage all changes via `git add -A`. Prefer explicit paths when possible."
        ),
    )


class GitCommitOutput(BaseModel):
    sha: str
    files_committed: list[str]
    branch: str
    latency_ms: int = 0


class GitCommit(Tool):
    name = "git_commit"
    description = (
        "Commit the agent's edits inside the spike output dir. Author is "
        "the registered agent identity. Pass an empty `paths` list to stage "
        "every change, or list specific files. Returns the new commit SHA so "
        "the agent can reference it in subsequent reasoning."
    )
    input_schema = GitCommitInput
    output_schema = GitCommitOutput
    mutates_target = True
    verifier: str | None = "git_status"

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        params = ensure_input(input, GitCommitInput)
        started = time.perf_counter_ns()
        cwd = output_root(ctx)

        # Stage.
        if params.paths:
            add_proc = await _run_git(["add", "--", *params.paths], cwd=cwd)
        else:
            add_proc = await _run_git(["add", "-A"], cwd=cwd)
        if add_proc.returncode != 0:
            raise RuntimeError(f"git add failed: {add_proc.stderr.strip()}")

        # Author identity ties the commit to this session.
        ident = agent_git_identity()
        author = f"{ident.name} <{ctx.session.id}@{ident.email_domain}>"
        commit_args = [
            "-c",
            f"user.name={ident.name}",
            "-c",
            f"user.email={ctx.session.id}@{ident.email_domain}",
            "commit",
            "--author",
            author,
            "--allow-empty-message",  # rare; defend against empty messages mid-iteration
            "-m",
            params.message,
        ]
        commit_proc = await _run_git(commit_args, cwd=cwd)
        if commit_proc.returncode != 0:
            # "nothing to commit" is a soft failure — surface as an empty SHA
            # so the agent can read the stderr and decide.
            stderr = commit_proc.stderr.strip()
            if "nothing to commit" in stderr or "nothing added to commit" in stderr:
                return GitCommitOutput(
                    sha="",
                    files_committed=[],
                    branch=(await _current_branch(cwd)),
                    latency_ms=elapsed_ms(started),
                )
            raise RuntimeError(f"git commit failed: {stderr}")

        sha_proc = await _run_git(["rev-parse", "HEAD"], cwd=cwd)
        files_proc = await _run_git(["show", "--name-only", "--pretty=", "HEAD"], cwd=cwd)
        out = GitCommitOutput(
            sha=sha_proc.stdout.strip(),
            files_committed=[ln.strip() for ln in files_proc.stdout.splitlines() if ln.strip()],
            branch=(await _current_branch(cwd)),
            latency_ms=elapsed_ms(started),
        )
        log.info("spike.git_commit", sha=out.sha[:12], files=len(out.files_committed))
        return out


# ─────────────────────── git_revert ───────────────────────


class GitRevertInput(BaseModel):
    sha: str = Field(
        ...,
        description=(
            "Commit SHA to revert. Must be reachable from HEAD on the agent's "
            "branch — operator branches are off-limits."
        ),
    )


class GitRevertOutput(BaseModel):
    revert_sha: str
    reverted_sha: str
    branch: str
    latency_ms: int = 0


class GitRevert(Tool):
    name = "git_revert"
    description = (
        "Revert a prior commit on the agent's branch (creates a new commit "
        "that undoes the target). Use when a verifier (e.g. install_module) "
        "fails after a commit — revert, then iterate."
    )
    input_schema = GitRevertInput
    output_schema = GitRevertOutput
    mutates_target = True
    verifier: str | None = "git_status"

    async def call(self, input: BaseModel, ctx: ToolContext) -> BaseModel:
        params = ensure_input(input, GitRevertInput)
        started = time.perf_counter_ns()
        cwd = output_root(ctx)
        # Sanity: reject reverts on a branch that isn't the agent's.
        ident = agent_git_identity()
        branch = await _current_branch(cwd)
        if not branch.startswith(ident.branch_prefix):
            raise RuntimeError(
                f"git_revert refused: branch {branch!r} is not an agent branch "
                f"(must start with {ident.branch_prefix!r})"
            )
        revert_proc = await _run_git(
            [
                "-c",
                f"user.name={ident.name}",
                "-c",
                f"user.email={ctx.session.id}@{ident.email_domain}",
                "revert",
                "--no-edit",
                params.sha,
            ],
            cwd=cwd,
        )
        if revert_proc.returncode != 0:
            raise RuntimeError(f"git revert failed: {revert_proc.stderr.strip()}")
        sha_proc = await _run_git(["rev-parse", "HEAD"], cwd=cwd)
        out = GitRevertOutput(
            revert_sha=sha_proc.stdout.strip(),
            reverted_sha=params.sha,
            branch=branch,
            latency_ms=elapsed_ms(started),
        )
        log.info("spike.git_revert", reverted=params.sha[:12], new_sha=out.revert_sha[:12])
        return out


# ─────────────────────── helpers ──────────────────────────


class _Completed:
    def __init__(self, *, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _run_git(args: list[str], *, cwd: Path) -> _Completed:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return _Completed(
        returncode=proc.returncode or 0,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
    )


async def _current_branch(cwd: Path) -> str:
    proc = await _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    return proc.stdout.strip() or "(detached)"
