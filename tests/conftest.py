"""Shared setup for the repo's test suite.

**Why the tests live at the repo root rather than inside `reviewer/`.** Each
component's Docker build context *is* its own folder (`docker build ./reviewer`),
so anything under `reviewer/` ships inside the production image. Tests, their
fixtures and their dev-only dependencies have no business in a CI reviewer
container, and a root-level suite can also cover `collector/` without either
component importing the other.

The cost is one wrinkle handled here: the reviewer's modules import each other by
top-level name (`from core.utils... import`, `from dto.general import`), because
`main.py` runs with `reviewer/` as its working directory. So `reviewer/` goes on
`sys.path` before any test imports anything. pytest loads conftest first, which is
what makes that work.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REVIEWER = REPO_ROOT / "reviewer"
COLLECTOR = REPO_ROOT / "collector"

# Prepend, not append: a name collision with an installed package should resolve
# to the code under test.
if str(REVIEWER) not in sys.path:
    sys.path.insert(0, str(REVIEWER))

# The collector's modules import each other by top-level name for the same reason
# the reviewer's do (`import storage`, `import engagement` — gunicorn runs with
# `collector/` as the working directory), so it needs the same treatment. Only
# modules that touch no database at import time are testable this way:
# ``engagement`` and ``digest`` are pure, while ``main`` runs Alembic on import
# and is deliberately never imported here.
if str(COLLECTOR) not in sys.path:
    sys.path.insert(0, str(COLLECTOR))

import pytest  # noqa: E402  (after the path setup, deliberately)


@pytest.fixture(autouse=True)
def _no_ambient_llm_credentials(monkeypatch):
    """Make an accidental live LLM call fail loudly instead of quietly working.

    Every test here uses a fake or scripted model. If one ever reaches a real
    provider it should blow up in CI rather than spend tokens and pass — and a
    developer running the suite locally has `reviewer/.env` sitting right there
    with working credentials in it.
    """
    for key in ("OPENROUTER_API_KEY", "FALLBACK_LLM_API_KEY"):
        monkeypatch.setenv(key, "test-suite-must-not-call-a-provider")
    monkeypatch.setenv("DEFAULT_LLM_MODEL", "test/model")
    monkeypatch.delenv("FALLBACK_LLM_MODEL", raising=False)


@pytest.fixture(autouse=True)
def _reset_provider_latch():
    """Clear the gateway's process-wide provider state between tests.

    `LLMGateway` keeps the failover latch as *class* state, deliberately: a run's
    reviewers share it so they don't each rediscover a rate limit. That makes it
    leak across tests, where one test's simulated failover would silently change
    the provider another test resolves.
    """
    from core.llm.gateway import LLMGateway

    LLMGateway.reset()
    yield
    LLMGateway.reset()


@pytest.fixture
def git_origin(tmp_path):
    """A real local git repository: `main` with 3 commits, a feature branch with 4.

    Served over `file://`, so the clone ladder is exercised against actual git
    rather than a mock. Returns `(origin_path, feature_tip_sha, marker_sha)` where
    the marker stands in for a previously-reviewed commit.
    """
    import subprocess

    env = {
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ.get("PATH", ""),
    }

    def git(*args, cwd=None):
        result = subprocess.run(["git", *args], cwd=cwd, env=env,
                                capture_output=True, text=True)
        assert result.returncode == 0, f"{args}: {result.stderr}"
        return result.stdout.strip()

    origin = tmp_path / "origin"
    git("init", "-q", "--initial-branch=main", str(origin))
    for i in (1, 2, 3):
        (origin / f"m{i}.txt").write_text(f"main {i}")
        git("add", ".", cwd=origin)
        git("commit", "-qm", f"main {i}", cwd=origin)
    git("checkout", "-qb", "feature/x", cwd=origin)
    for i in (1, 2, 3, 4):
        (origin / f"f{i}.go").write_text(f"package p // feat {i}\n")
        git("add", ".", cwd=origin)
        git("commit", "-qm", f"feat {i}", cwd=origin)

    return origin, git("rev-parse", "feature/x", cwd=origin), git("rev-parse", "feature/x~2", cwd=origin)


@pytest.fixture
def checkout(tmp_path):
    """A small on-disk tree standing in for a cloned source branch."""
    root = tmp_path / "checkout"
    (root / "svc").mkdir(parents=True)
    (root / "svc" / "order.go").write_text(
        "package svc\n\nfunc Save() error {\n\treturn nil\n}\n"
    )
    (root / ".aid").mkdir()
    (root / ".aid" / "service.md").write_text("- Always check errors from Save().\n")
    return root
