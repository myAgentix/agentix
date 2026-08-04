"""git_status doubles as the write_file / apply_patch verifier, so its ``ok`` field must reflect whether the
working tree actually changed. Without it, SafetyGate reads a missing ``ok`` (defaults True) and passes a
no-op / failed write vacuously — voiding the verify->rollback guarantee for the edit tools.
"""

from __future__ import annotations

import subprocess

import agentix.tools.spike.git_ops as git_ops
from agentix.tools.spike.git_ops import GitStatus, GitStatusInput


def _git(cwd, *args) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


async def test_git_status_ok_reflects_working_tree_change(tmp_path, monkeypatch) -> None:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t.local")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "f.txt").write_text("one\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")

    monkeypatch.setattr(git_ops, "output_root", lambda ctx: tmp_path)

    # Clean tree = the guarded write changed nothing -> ok=False so SafetyGate treats it as drift.
    clean = await GitStatus().call(GitStatusInput(), ctx=None)
    assert clean.clean is True
    assert clean.ok is False

    # A real edit dirties the tree -> ok=True (the write took effect).
    (tmp_path / "f.txt").write_text("two\n")
    dirty = await GitStatus().call(GitStatusInput(), ctx=None)
    assert dirty.clean is False
    assert dirty.ok is True
