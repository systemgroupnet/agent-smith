"""The shallow-clone ladder, against a real local git repository.

Every reviewer reads the checkout, so a clone that silently produces the wrong
tree produces a whole run of findings anchored to the wrong lines. These run
against actual git over `file://` rather than a mock, because the things worth
testing here are git's semantics: what `--shallow-exclude` includes, whether a
detached checkout of an older SHA works inside a shallow window, and what happens
when a branch no longer exists on the remote.
"""

import subprocess

import pytest

from core.utils import repo_clone as rc
from core.utils.repo_clone import _redact, clone_source_branch, git_available

#: Captured at import, before the autouse fixture below repoints it at a local
#: `file://` origin. Importing it inside the test would get the patched version —
#: which is exactly how an earlier version of this test passed while asserting
#: nothing.
_REAL_AUTHENTICATED_URL = rc._authenticated_url


def _git(*args, cwd=None):
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"{args}: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture(autouse=True)
def _local_origin(monkeypatch, git_origin):
    """Point the clone helper at the local origin instead of building an https URL."""
    origin, _tip, _marker = git_origin
    monkeypatch.setattr(rc, "_authenticated_url",
                        lambda gitlab_url, project_path, token: f"file://{origin}")


def test_git_is_available():
    """The image must ship git — python:*-slim does not."""
    assert git_available(), "no usable git binary on PATH"


def test_shallow_exclude_takes_the_branchs_own_commits(git_origin):
    _origin, tip, marker = git_origin
    clone = clone_source_branch("https://gl.example", "g/p", "tok", "feature/x",
                                target_branch="main", head_sha=tip)
    assert clone is not None
    try:
        assert clone.method == "shallow-exclude"
        assert clone.commits == 4, "should hold the 4 feature commits and stop at the branch point"
        assert clone.head == tip
        assert (
            _git("diff", "--name-only", marker, "HEAD", cwd=clone.path).split()
            == ["f3.go", "f4.go"]
        ), "a previously-reviewed marker commit must be inside the window"
    finally:
        clone.cleanup()


def test_checkout_is_pinned_to_the_reviewed_sha(git_origin):
    """A push during the run moves the tip; the review is of the API's SHA."""
    origin, tip, _marker = git_origin
    (origin / "f5.go").write_text("package p // feat 5\n")
    _git("add", ".", cwd=origin)
    _git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "feat 5", cwd=origin)
    assert _git("rev-parse", "feature/x", cwd=origin) != tip

    clone = clone_source_branch("https://gl.example", "g/p", "tok", "feature/x",
                                target_branch="main", head_sha=tip)
    assert clone is not None
    try:
        assert clone.head == tip, "must pin to the reviewed SHA, not the moved tip"
        assert not (__import__("pathlib").Path(clone.path) / "f5.go").exists()
    finally:
        clone.cleanup()


def test_falls_through_to_depth_one_when_deepen_not_is_unsupported(monkeypatch, git_origin):
    _origin, tip, _marker = git_origin
    real = rc._run_git

    def no_deepen_not(args, cwd=None):
        if any(str(a).startswith("--shallow-exclude") for a in args):
            return subprocess.CompletedProcess(args, 1, "", "fatal: unsupported capability deepen-not")
        return real(args, cwd=cwd)

    monkeypatch.setattr(rc, "_run_git", no_deepen_not)
    clone = clone_source_branch("https://gl.example", "g/p", "tok", "feature/x",
                                target_branch="main", head_sha=tip)
    assert clone is not None
    try:
        assert clone.method == "depth-1" and clone.commits == 1
    finally:
        clone.cleanup()


def test_recovers_via_the_mr_head_ref_when_the_branch_is_gone(monkeypatch, git_origin):
    """The rung that earned its keep on the first real MR: a deleted source branch."""
    origin, tip, _marker = git_origin
    _git("update-ref", "refs/merge-requests/77/head", tip, cwd=origin)
    real = rc._run_git

    def no_branch(args, cwd=None):
        if args and args[0] == "clone":
            return subprocess.CompletedProcess(
                args, 128, "", "fatal: Remote branch not found in upstream origin")
        return real(args, cwd=cwd)

    monkeypatch.setattr(rc, "_run_git", no_branch)
    clone = clone_source_branch("https://gl.example", "g/p", "tok", "deleted/branch",
                                target_branch="main", mr_iid="77", head_sha=tip)
    assert clone is not None
    try:
        assert clone.method == "mr-ref" and clone.head == tip
    finally:
        clone.cleanup()


def test_returns_none_when_every_rung_fails(monkeypatch):
    real = rc._run_git
    monkeypatch.setattr(rc, "_run_git", lambda args, cwd=None: (
        real(args, cwd=cwd) if args and args[0] == "--version"
        else subprocess.CompletedProcess(args, 1, "", "fatal: boom")))
    assert clone_source_branch("https://gl.example", "g/p", "tok", "feature/x",
                               target_branch="main", mr_iid="77") is None


def test_cleanup_removes_the_temp_root_too(git_origin):
    """`path` is `<root>/repo`; removing only `path` leaked a directory per run."""
    import os

    _origin, tip, _marker = git_origin
    clone = clone_source_branch("https://gl.example", "g/p", "tok", "feature/x",
                                target_branch="main", head_sha=tip)
    assert clone is not None and clone.root
    clone.cleanup()
    assert not os.path.exists(clone.path)
    assert not os.path.exists(clone.root), f"temp root leaked: {clone.root}"


def test_credentials_never_reach_a_log(monkeypatch):
    """git echoes the clone URL in most of its errors, and it carries a token."""
    monkeypatch.setenv("GITLAB_TOKEN", "glpat-SUPERSECRET")
    built = _REAL_AUTHENTICATED_URL("https://gl.example.net", "group/proj", "glpat-SUPERSECRET")
    assert built == "https://oauth2:glpat-SUPERSECRET@gl.example.net/group/proj.git"

    redacted = _redact(f"fatal: could not read from {built}")
    assert "SUPERSECRET" not in redacted
    assert "***" in redacted

    # and the generic form, for a credential this process never saw
    assert _redact("fatal: https://someone:hunter2@host/x.git") == (
        "fatal: https://***@host/x.git")


def test_lfs_is_never_smudged():
    """A real monorepo carries 203 MiB of LFS objects no reviewer will read."""
    import inspect

    assert 'GIT_LFS_SKIP_SMUDGE"] = "1"' in inspect.getsource(rc._run_git)
