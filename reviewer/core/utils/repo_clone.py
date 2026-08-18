"""A shallow working copy of the MR's source branch.

Until now the reviewer had no checkout: file content came from the GitLab REST
API and code search came from the cocoindex MCP index. Both work, and both have
the same structural weakness — they answer about a *ref you have to name*, and
naming it wrong is silent. The index in particular answers from the server's
base branch when the `branch` argument is missing, so a symbol the MR added
looks undefined and a signature it changed looks unchanged, which is the exact
false-positive class the shared MR context block exists to suppress.

A checkout has no ref to get wrong. `grep` over this directory is the MR's code
by construction, at the commit under review, with no session to drop, no tool
schema to violate and no 60-second stall.

**Depth is decoupled from the incremental review.** Diffs still come from the
MR API and must: `build_diff_position_map`/`resolve_inline_position` anchor every
inline comment against GitLab's own ``diff_refs``, and a locally computed diff
can legitimately disagree (rename detection, whitespace settings, merge-base vs
straight), which would put the in-diff gate and GitLab's line codes out of sync.
So this clone is a *read surface*, never a diff source.

Given that, the depth only has to satisfy one requirement: contain the commit
the API says we are reviewing. ``--shallow-exclude=<target-branch>`` is the flag
that expresses it exactly — "history excluding commits reachable from that
branch", i.e. the source branch's own commits and nothing else. No depth to
guess, it self-adjusts to however many commits piled up between pipeline runs,
and because a marker SHA is by definition a commit on this branch it is always
inside the window (verified: ``git diff <marker> HEAD`` and ``git log
<marker>..HEAD`` both resolve inside such a clone). ``--depth=1`` is the
fallback for a server that won't serve ``deepen-not``; it costs about the same
on the wire, since the blobs at HEAD dominate either way.

**Who the clone authenticates as** is a separate question, and the answer is
``GITLAB_TOKEN`` — the same token the run uses for the REST API, embedded in the
URL below. The reviewed project's job runs with ``GIT_STRATEGY: none`` (there is
no workspace checkout for this git to inherit a credential from, and letting the
runner clone the same monorepo the reviewer is about to clone would double the
slowest step in the pipeline), so this is the only git in the job and it carries
its own credential. That token is sufficient by construction: posting a comment
needs an ``api``-scoped token, and ``api`` already covers Git-over-HTTPS
repository read.

A pipeline-scoped ``~/.netrc`` written by the job — the house pattern for git in
this organisation's CI — was considered and rejected: it buys only hygiene (a
short-lived scoped credential, and one that stays out of argv) at the cost of a
second way for the clone to fail, on the one path where failing takes the whole
review with it. Recording the non-obvious part in case anyone revisits it: git
reads ``~/.netrc`` through libcurl's ``CURLOPT_NETRC`` in *optional* mode, which
is consulted **only when the URL carries no userinfo**. Verified against a local
smart-HTTP git server — a valid netrc plus a wrong ``oauth2:…@`` credential fails
outright. So a token in the URL does not merely outrank a netrc, it makes the
netrc unreachable, and using one would mean deliberately building a
credential-free URL rather than just writing the file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit


#: Wall-clock budget for a clone. A monorepo checkout over a corporate network is
#: slow but not unbounded; past this the review is better off degrading to the
#: API-only path than spending its whole CI window on git.
#:
#: Measured on acme/shop (462 MiB repo, 28,229 files in the tree): a
#: ``--shallow-exclude`` clone took **403 s** over a WAN link, of which ~190 s was
#: the server packing and sending ~40 MiB and **3 s** was writing the working
#: tree. So this is a *download* budget, not a disk one, and it scales with the
#: link rather than the repo's file count. A runner in the same datacentre as
#: GitLab should be far quicker; 900 s is chosen so the slow case degrades to a
#: graph-mode review by timing out rather than by silently exceeding a 300 s
#: default on every single run.
CLONE_TIMEOUT_ENV = "REPO_CLONE_TIMEOUT_S"
DEFAULT_CLONE_TIMEOUT_S = 900


def _clone_timeout() -> int:
    raw = (os.getenv(CLONE_TIMEOUT_ENV) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_CLONE_TIMEOUT_S
    return value if value > 0 else DEFAULT_CLONE_TIMEOUT_S


@dataclass
class RepoClone:
    """A checkout of the source branch, plus how it was obtained."""

    path: str
    #: "shallow-exclude" | "depth-1" | "mr-ref" — which rung of the ladder worked.
    method: str
    #: The commit actually checked out. Compared against the API's head_sha by the
    #: caller: a mismatch means someone pushed mid-run, and the review is of the
    #: API's SHA, so the checkout is pinned to it whenever it is in the window.
    head: str
    #: Commits present in the shallow window (1 for a depth-1 clone).
    commits: int
    #: The temp directory that *contains* the checkout, so cleanup removes the
    #: whole thing. ``path`` is ``<root>/repo`` because git refuses to clone into a
    #: non-empty directory and ``mkdtemp`` has already created one; removing only
    #: ``path`` left an empty ``<root>`` behind on every single run. Harmless in a
    #: one-shot CI container, an actual leak in web mode, which serves many reviews
    #: from one process. Observed: 23 stray directories after a test session.
    root: Optional[str] = None

    def cleanup(self) -> None:
        """Remove the checkout and its temp directory.

        Never raises — a crash in cleanup would mask the run's real outcome.
        """
        for target in (self.path, self.root):
            if not target:
                continue
            try:
                shutil.rmtree(target, ignore_errors=True)
            except Exception:  # pragma: no cover - ignore_errors already covers it
                pass


def _redact(text: str) -> str:
    """Strip credentials out of anything that might reach a log.

    The clone URL carries a token in its userinfo, and git echoes the URL back in
    most of its error messages. Every log line and every captured stderr goes
    through this.
    """
    if not text:
        return text
    out = text
    for token in filter(None, (os.getenv("GITLAB_TOKEN"), os.getenv("CI_JOB_TOKEN"))):
        out = out.replace(token, "***")
    # Any remaining scheme://user:pass@host form, whatever the credential was.
    import re

    return re.sub(r"(https?://)[^/@\s]*@", r"\1***@", out)


def _authenticated_url(gitlab_url: str, project_path: str, token: str) -> str:
    """Build the https clone URL with credentials embedded.

    Credentials in the URL land in this process's argv, which is readable by the
    same user via ``/proc`` (not via ``ps`` for other users). The alternative —
    injecting an ``Authorization`` header through ``GIT_CONFIG_*`` env vars — is
    marginally better on that axis but is another thing that can be subtly wrong
    against a given GitLab, and a clone that fails takes the whole review with
    it. In a single-tenant one-shot CI container the argv exposure is acceptable;
    what is *not* acceptable is printing it, so nothing here is logged unredacted.
    """
    parts = urlsplit(gitlab_url if "://" in gitlab_url else f"https://{gitlab_url}")
    host = parts.netloc.split("@")[-1]
    path = f"/{project_path.strip('/')}.git"
    return urlunsplit((parts.scheme or "https", f"oauth2:{token}@{host}", path, "", ""))


def _run_git(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    """Run git with prompts disabled and output captured.

    ``GIT_TERMINAL_PROMPT=0`` matters: a bad token makes git block on a username
    prompt forever otherwise, and in CI that is a hung job rather than an error.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    # Never download LFS content. acme-shop carries **203 MiB of LFS objects**
    # on top of a 462 MiB repository, and on a runner that has git-lfs installed a
    # plain clone smudges every one of them — several minutes of download for
    # binaries a code reviewer will never read. The pointer files are still checked
    # out, so a reviewer that greps one sees a pointer rather than a truncated blob.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=_clone_timeout(),
    )


def git_available() -> bool:
    """Whether a usable git binary is on PATH (the image must install one)."""
    try:
        return _run_git(["--version"]).returncode == 0
    except Exception:
        return False


def _describe(result: subprocess.CompletedProcess) -> str:
    return _redact((result.stderr or result.stdout or "").strip().splitlines()[-1:][0]) if (
        result.stderr or result.stdout
    ) else f"exit {result.returncode}"


def _finish(dest: str, method: str, head_sha: Optional[str], root: Optional[str] = None) -> Optional[RepoClone]:
    """Pin the checkout to the reviewed commit and describe the result.

    The API's ``head_sha`` is authoritative: the diffs, the position maps and the
    in-diff gate are all built against it, so a checkout of a *newer* tip (someone
    pushed while the review was running) would have the reviewers reading code
    that no finding can be anchored to. When that SHA is in the window we check it
    out; when it isn't (a depth-1 clone whose tip has moved) we keep the tip and
    say so, because a partly-wrong read surface still beats no read surface.
    """
    if head_sha:
        checkout = _run_git(["checkout", "--quiet", "--detach", head_sha], cwd=dest)
        if checkout.returncode != 0:
            print(
                f"  ⚠️  [clone] reviewed commit {head_sha[:8]} is not in the shallow "
                f"window; staying on the branch tip ({_describe(checkout)})"
            )
    head = _run_git(["rev-parse", "HEAD"], cwd=dest)
    if head.returncode != 0:
        print(f"  ❌ [clone] checkout has no HEAD: {_describe(head)}")
        shutil.rmtree(dest, ignore_errors=True)
        return None
    count = _run_git(["rev-list", "--count", "HEAD"], cwd=dest)
    commits = int(count.stdout.strip()) if count.returncode == 0 and count.stdout.strip().isdigit() else 0
    resolved = head.stdout.strip()
    print(f"  ✅ [clone] {method}: {commits} commit(s), HEAD {resolved[:8]} at {dest}")
    return RepoClone(path=dest, method=method, head=resolved, commits=commits, root=root)


def clone_source_branch(
    gitlab_url: str,
    project_path: str,
    token: str,
    source_branch: str,
    target_branch: Optional[str] = None,
    mr_iid: Optional[str] = None,
    head_sha: Optional[str] = None,
    dest: Optional[str] = None,
) -> Optional[RepoClone]:
    """Check out the MR's source branch as shallowly as the server allows.

    Tries three rungs, each a complete answer on its own, and returns the first
    that produces a working tree:

    1. ``--shallow-exclude=<target>`` — the branch's own commits, stopping at the
       branch point. Preferred because it needs no depth guess and always
       contains any previously-reviewed marker commit, so a local
       ``git diff <marker> HEAD`` is available if we ever want one.
    2. ``--depth=1`` — one commit, full tree. Used when the server won't serve
       ``deepen-not`` (the capability behind ``--shallow-exclude``).
    3. ``refs/merge-requests/<iid>/head`` at depth 1 — GitLab creates this ref for
       every MR, so it still resolves after someone deletes the source branch
       mid-review, which is the one case the first two both fail on.

    Returns ``None`` when every rung fails; the caller decides whether that is
    fatal (the deep-agent path needs a checkout) or merely a lost optimisation
    (the API-only path does not).
    """
    if not git_available():
        print("  ❌ [clone] no git binary on PATH — install git in the image")
        return None
    if not (gitlab_url and project_path and token and source_branch):
        print("  ⚠️  [clone] missing clone parameters; skipping checkout")
        return None

    url = _authenticated_url(gitlab_url, project_path, token)
    root = dest or tempfile.mkdtemp(prefix="codereview-src-")
    # git refuses to clone into a non-empty directory, and mkdtemp created it.
    path = os.path.join(root, "repo")

    common = ["--single-branch", "--no-tags", "--quiet"]
    ladder: List[tuple] = []
    if target_branch:
        ladder.append((
            "shallow-exclude",
            ["clone", *common, "--branch", source_branch,
             f"--shallow-exclude={target_branch}", url, path],
        ))
    ladder.append((
        "depth-1",
        ["clone", *common, "--depth=1", "--branch", source_branch, url, path],
    ))

    for method, args in ladder:
        result = _run_git(args)
        if result.returncode == 0:
            return _finish(path, method, head_sha, root)
        print(f"  ⚠️  [clone] {method} failed: {_describe(result)}")
        shutil.rmtree(path, ignore_errors=True)

    # Last rung: the MR head ref. Needs an init+fetch rather than a clone, since
    # `git clone --branch` only accepts a branch or tag name, not an arbitrary ref.
    if mr_iid:
        ref = f"refs/merge-requests/{mr_iid}/head"
        os.makedirs(path, exist_ok=True)
        steps = (
            ["init", "--quiet", path],
            ["-C", path, "remote", "add", "origin", url],
            ["-C", path, "fetch", "--quiet", "--depth=1", "--no-tags", "origin", ref],
        )
        ok = True
        for args in steps:
            result = _run_git(list(args))
            if result.returncode != 0:
                print(f"  ⚠️  [clone] mr-ref step failed: {_describe(result)}")
                ok = False
                break
        if ok:
            checkout = _run_git(["-C", path, "checkout", "--quiet", "--detach", "FETCH_HEAD"])
            if checkout.returncode == 0:
                return _finish(path, "mr-ref", head_sha, root)
            print(f"  ⚠️  [clone] mr-ref checkout failed: {_describe(checkout)}")
        shutil.rmtree(path, ignore_errors=True)

    shutil.rmtree(root, ignore_errors=True)
    print("  ❌ [clone] every clone strategy failed")
    return None
