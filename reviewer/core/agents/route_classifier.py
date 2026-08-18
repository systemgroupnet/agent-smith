"""Decides whether a Go file also needs the SQL reviewer.

Static classification is right about every file type here except one. An
extension tells you a `.sql` file is SQL and a `.ts` file is TypeScript, and it
tells you a `.go` file is Go — but it cannot tell you that the Go file is 200
lines of hand-built query. Go files in this codebase routinely carry query
strings, query builders, `Exec`/`Query` calls and embedded DDL, and the Go
reviewers are not database reviewers: nobody looks at that SQL unless something
decides to send it to `sql_reviewer`.

This is that decision, and it is deliberately **its own agent**. It was briefly a
clause in the review orchestrator's prompt, which required giving that agent
`grep` — a way to read code — and buried the decision in dispatch behaviour where
nothing could audit it. As its own agent the decision is a structured, logged
artifact: which files got a second reviewer, and why. (The orchestrator has since
been removed entirely; this outliving it is the point — it was the only routing
judgement in the system, and it never belonged to a dispatcher.)

**Deterministic evidence, LLM judgement.** Grepping for SQL keywords is something
code does perfectly and an LLM does expensively, so the signals are gathered here
with a regex over the checkout and only the *matched lines* go to the model. A
file with no signal at all never reaches the LLM — no evidence, no SQL, service
reviewer only. What the model is actually for is the part a regex is bad at:
telling a real query apart from the word "select" in a comment, a column name in
a struct tag, or a `SELECT 1` health check.
"""

from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Optional, Sequence, Tuple

from langchain_core.runnables import RunnableConfig

from core.agents.base_agent import BaseReviewAgent
from core.prompts.route_prompts import get_sql_router_prompt
from core.utils.llm_retry import describe_error, maybe_print_traceback
from dto.general import SqlRoutingResponse


#: Signals that a Go file might contain SQL worth a database reviewer's time.
#: Deliberately broad — this stage only decides *what to show the model*, and a
#: false positive here costs a few lines of prompt, not a review.
_SQL_SIGNALS = re.compile(
    r"""(?ix)
    \b(?:
        select\s|insert\s+into\b|update\s+\w+\s+set\b|delete\s+from\b|
        create\s+(?:table|index|view)\b|alter\s+table\b|drop\s+table\b|
        truncate\s+table\b|join\b|group\s+by\b|order\s+by\b|having\b|
        returning\b|on\s+conflict\b|with\s+recursive\b
    )
    | \.(?:Query|QueryRow|QueryContext|QueryRowContext|Exec|ExecContext|
          NamedExec|NamedQuery|Select|Get|Raw)\s*\(
    | \b(?:sqlx|gorm|pgx|squirrel|sq\.)\b
    """
)

#: Never read more than this from one file when gathering evidence — a generated
#: Go file can be enormous and the first matches are as informative as the last.
MAX_SCAN_BYTES = 400_000
#: Matched lines shown to the model per file. Enough to judge substance, bounded
#: so twenty Go files cannot blow up one request.
MAX_EVIDENCE_LINES = 12
#: Characters kept per matched line.
MAX_EVIDENCE_LINE_CHARS = 200


def gather_sql_evidence(path: str, clone_root: str) -> List[Tuple[int, str]]:
    """Return ``[(line_number, text)]`` for lines in ``path`` that look SQL-ish.

    Reads from the checkout, so it sees the merge request's own version of the
    file. A missing or unreadable file yields no evidence, which routes the file
    to the service reviewer alone — the same as a file with no SQL in it.
    """
    full = os.path.join(clone_root, path)
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            blob = fh.read(MAX_SCAN_BYTES)
    except (OSError, ValueError):
        return []

    hits: List[Tuple[int, str]] = []
    for number, line in enumerate(blob.splitlines(), start=1):
        if len(hits) >= MAX_EVIDENCE_LINES:
            break
        if _SQL_SIGNALS.search(line):
            hits.append((number, line.strip()[:MAX_EVIDENCE_LINE_CHARS]))
    return hits


def build_router_input(evidence: Dict[str, List[Tuple[int, str]]]) -> str:
    """Render the candidate Go files and their matched lines for the model."""
    parts: List[str] = [
        f"=== GO FILES WITH POSSIBLE SQL ({len(evidence)}) ===",
        "",
    ]
    for path, hits in evidence.items():
        parts.append(f"[{path}]")
        for number, text in hits:
            parts.append(f"  {number}: {text}")
        parts.append("")
    return "\n".join(parts)


class SqlRouterAgent(BaseReviewAgent[SqlRoutingResponse]):
    """Judges which Go files carry enough SQL to warrant the SQL reviewer."""

    def __init__(self, model_name: str = None, temperature: float = 0.0):
        super().__init__(
            model_name=model_name,
            temperature=temperature,
            output_model=SqlRoutingResponse,
        )

    def get_system_prompt(self) -> str:
        return get_sql_router_prompt()


def create_sql_router_agent(
    model_name: str = None, temperature: float = 0.0
) -> SqlRouterAgent:
    """Factory for the SQL routing classifier."""
    return SqlRouterAgent(model_name=model_name, temperature=temperature)


async def classify_sql_involvement(
    go_files: Sequence[str],
    clone_root: str,
    config: Optional[RunnableConfig] = None,
) -> Tuple[List[str], Optional[str]]:
    """Pick the Go files that should also go to ``sql_reviewer``.

    Args:
        go_files: Repository-relative paths of the Go files in the dispatch plan.
        clone_root: The checkout to read them from.
        config: Tracing run config, so this lands in the MR's session.

    Returns:
        ``(paths, error)`` — the files needing a SQL reviewer, and ``None`` on
        success or a description of the failure. **Fails closed**: on any failure
        no file gets the extra reviewer and the static assignment stands, because
        a spurious dispatch spends a whole review pass on a file with no SQL in
        it while a missed one costs a second opinion on SQL the Go reviewers are
        still looking at.
    """
    if not go_files or not clone_root:
        return [], None

    evidence = {}
    for path in go_files:
        hits = gather_sql_evidence(path, clone_root)
        if hits:
            evidence[path] = hits

    scanned = len(go_files)
    if not evidence:
        print(f"  🧭 SQL routing: no SQL signals in {scanned} Go file(s) — service reviewers only")
        return [], None

    print(
        f"  🧭 SQL routing: {len(evidence)} of {scanned} Go file(s) show SQL signals; "
        f"asking the classifier which are substantial"
    )

    started = time.time()
    try:
        agent = create_sql_router_agent()
        result = await agent.run(build_router_input(evidence), config=config)
        if result is None:
            raise ValueError(
                "routing classifier returned no structured output (the model did "
                "not call the response schema)"
            )
    except Exception as e:
        detail = describe_error(e)
        print(f"  ⚠️  SQL routing classifier failed after {time.time() - started:.2f}s: {detail}")
        maybe_print_traceback(e, indent="     ")
        print("  ℹ️  No extra SQL reviews this run — the static assignment stands")
        return [], f"sql routing classifier failed: {detail}"

    # Only ever *add* files we actually asked about: a model naming a path we did
    # not scan is either hallucinating or renaming, and neither should create a
    # dispatch.
    wanted: List[str] = []
    for decision in result.decisions:
        if not decision.needs_sql_review:
            continue
        path = decision.file_path.strip()
        if path in evidence:
            wanted.append(path)
        elif path:
            print(f"  ⚠️  SQL routing named {path}, which was not a candidate — ignored")

    unique = sorted(dict.fromkeys(wanted))
    if unique:
        print(f"  🧭 SQL routing: {len(unique)} file(s) also going to sql_reviewer")
        for decision in result.decisions:
            if decision.needs_sql_review and decision.file_path.strip() in unique:
                print(f"       ↳ {decision.file_path}: {decision.reason[:120]}")
    else:
        print("  🧭 SQL routing: classifier found no substantial SQL — service reviewers only")
    return unique, None
