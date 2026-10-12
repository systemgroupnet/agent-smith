"""Where a reviewer's findings actually land.

Every reviewer reports through ``record_findings``, a tool closed over a
run-scoped :class:`FindingsSink`, and the issue is normalised through
``ReviewIssue`` at that moment. Nothing between the reviewer and the comment
pipeline restates it.

This began as a defence against an orchestrator. In `deepagents` a subagent
reports back *through the parent* — its result becomes a `ToolMessage` in the
parent's conversation — so the natural design had an orchestrator re-writing every
finding it relayed, which this codebase already knows the cost of:
`restore_issue_severities` exists because the comment reviewer's synthesis
silently relabelled real highs as low on three production runs, and the posting
loop's position safety net exists because a rewritten ``file_name``/``line``
misses the in-diff gate and the finding is lost.

The orchestrator is gone, and the sink is still the right shape for two reasons
that have nothing to do with it:

* **It survives truncation.** A reviewer that hits its model-call limit or its
  watchdog has already recorded whatever it reported; reading a
  ``structured_response`` off a cancelled invocation would get nothing. Findings
  are banked as they are made.
* **It is the coverage ledger.** A reviewer that finds nothing still calls this
  with an empty list, so "reviewed and clean" is distinguishable from "never ran"
  — and a reviewer that returns successfully without ever calling it is reported
  as a failed file rather than a silent gap.

A closure rather than `ToolRuntime`/`context_schema`: passing per-run dependencies
through context is the idiomatic LangChain channel, but the sink is built per run
anyway (the prompts embed per-run MR context), and a closure cannot fail to reach
it whatever the middleware does with context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field, field_validator

from dto.general import ReviewIssue, coerce_line_number, coerce_severity


#: Per-file ceiling. A reviewer looping on one file cannot flood the run; the
#: comment reviewer and the verbosity cap would discard the tail anyway, and the
#: in-diff gate has to resolve every one of them first.
MAX_FINDINGS_PER_FILE = 50
#: Whole-run ceiling, across every subagent.
MAX_FINDINGS_TOTAL = 500


class RecordedFinding(BaseModel):
    """One finding as a reviewer subagent reports it.

    Deliberately the same field names as :class:`dto.general.ReviewIssue` so the
    values pass straight into it and inherit its lenient coercions (a `line` of
    ``"12-15"`` becomes 12, a non-numeric one becomes ``None``) rather than
    getting a second, subtly different parser here.
    """

    problem: str = Field(description="What is wrong, and why it matters at runtime.")
    suggestion: str = Field(
        default="",
        description=(
            "Replacement CODE ONLY for the offending line — no prose, no fences, no "
            "line-number gutter. Leave empty when there is no concrete code to apply."
        ),
    )
    line: Optional[int] = Field(
        default=None,
        description=(
            "1-based line number in the file as read_file numbered it. This is the "
            "source-branch numbering the merge request anchors comments to."
        ),
    )
    severity: str = Field(default="low", description="high, medium, or low.")
    file_name: str = Field(
        default="",
        description=(
            "Only set this when the finding is in a DIFFERENT file from the one you "
            "were asked to review. Leave empty otherwise."
        ),
    )

    # This is the *first* validator a finding meets in deep mode, and a tool call
    # that fail-validates loses the whole file's findings — the model gets a
    # retry-with-error and may well spend its remaining iterations on it. So these
    # coercions must be at least as forgiving as ReviewIssue's, and they are the
    # same functions rather than a second stricter copy: a model that answers
    # `line: "n/a"` or `line: "12-15"` or `severity: "CRITICAL"` is absorbed here
    # exactly as it would be there.
    @field_validator("line", mode="before")
    @classmethod
    def _coerce_line(cls, value):
        return coerce_line_number(value)

    @field_validator("severity", mode="before")
    @classmethod
    def _coerce_severity(cls, value):
        return coerce_severity(value)

    @field_validator("problem", "suggestion", "file_name", mode="before")
    @classmethod
    def _coerce_text(cls, value):
        return "" if value is None else str(value)


class RecordFindingsArgs(BaseModel):
    """Arguments for the ``record_findings`` tool."""

    file_path: str = Field(
        description="Repository-relative path of the file you were asked to review."
    )
    findings: List[RecordedFinding] = Field(
        default_factory=list,
        description=(
            "Every issue you found in that file. Pass an empty list when the file is "
            "clean — that is a normal result and still records the file as reviewed."
        ),
    )


#: A path-shaped token: a name with a 2–5 character extension starting with a
#: letter, optionally with directories. The extension rule keeps prose ("e.g.",
#: "i.e.") and numbers ("0.35", "v1.2") from reading as paths.
_PATH_TOKEN = re.compile(r"[\w.\-/]*\w\.[A-Za-z][A-Za-z0-9]{1,4}\b")


def finding_file_name(reported: Optional[str], own_path: str) -> str:
    """The ``file_name`` a finding is stored under.

    Normally exactly what the reviewer reported (the in-diff gate resolves bare
    and suffix forms against the MR), or the file it reviewed when it reported
    none. But a reviewer explaining a cross-file finding sometimes writes prose
    into the field. Observed on this agent's own MR: ``comment_prompts.py,
    common.py (VERIFY_BEFORE_REPORTING, check 4 has the carve-out this rule
    lacks; …)``. No gate can resolve that, so a real finding was dropped as an
    unknown file. Such a value is reduced to the first path it names, the file it
    is about, since prose names the defective file first; with no path in it at
    all, to the file under review. The gate still decides whether that path and
    line are in the MR.
    """
    raw = (reported or "").strip()
    if not raw:
        return own_path
    if not re.search(r"[\s,;()]", raw):
        return raw
    match = _PATH_TOKEN.search(raw)
    return FindingsSink._normalise(match.group(0)) if match else own_path


@dataclass
class FindingsSink:
    """Run-scoped collector for everything the reviewer subagents report."""

    #: file_path -> issues, in the order files were reported. Values are plain
    #: dicts (``ReviewIssue.model_dump()``), which is what the graph path also
    #: produces and what every consumer downstream expects.
    findings: Dict[str, List[dict]] = field(default_factory=dict)
    #: Files a subagent reported on at all, findings or not — the coverage ledger.
    reviewed: List[str] = field(default_factory=list)
    #: Findings refused by a cap, so truncation is never silent.
    over_cap: int = 0
    #: Findings whose payload could not be validated at all.
    rejected: int = 0

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.findings.values())

    @property
    def reviewed_files(self) -> set:
        return set(self.reviewed)

    #: Paths the reviewers use to *read* are rooted at the checkout, so a model that
    #: reports the path it read reports ``/svc/pay.go`` where the dispatch plan says
    #: ``svc/pay.go``. Both name the same file; the leading slash is the virtual
    #: filesystem's root, not part of the repository path. Observed live: a reviewer
    #: recorded a correct finding under ``/svc/pay.go`` and the pass then reported
    #: ``reviewed: {'/svc/pay.go'} | failed: ['svc/pay.go']`` — the same file counted
    #: as both, because coverage is exact set membership against the plan. That means
    #: a non-zero exit and a held marker for a file that was in fact reviewed, and
    #: downstream it makes the in-diff gate match by basename rather than by path.
    @staticmethod
    def _normalise(path: str) -> str:
        """Repository-relative form of whatever path the model reported (see also
        :func:`finding_file_name` for a finding's own ``file_name``)."""
        cleaned = (path or "").strip().replace("\\", "/")
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        return cleaned.lstrip("/")

    def record(self, file_path: str, findings: List[RecordedFinding]) -> str:
        """Store one subagent's report and return the line the model sees back."""
        path = self._normalise(file_path)
        if not path:
            self.rejected += len(findings or [])
            return (
                "Rejected: file_path was empty. Call record_findings again with the "
                "repository-relative path of the file you reviewed."
            )

        if path not in self.findings:
            self.findings[path] = []
            self.reviewed.append(path)

        stored = 0
        for item in findings or []:
            if len(self.findings[path]) >= MAX_FINDINGS_PER_FILE or self.total >= MAX_FINDINGS_TOTAL:
                self.over_cap += 1
                continue
            try:
                # Normalise through ReviewIssue so severity/line coercion and the
                # dict shape match the graph path exactly.
                issue = ReviewIssue(
                    problem=item.problem,
                    suggestion=item.suggestion,
                    file_name=finding_file_name(item.file_name, path),
                    line=item.line,
                    severity=item.severity,
                )
            except Exception:
                self.rejected += 1
                continue
            if not issue.problem.strip():
                # A finding with no description cannot be posted and cannot be
                # judged; dropping it here keeps it out of the gate's warnings.
                self.rejected += 1
                continue
            self.findings[path].append(issue.model_dump())
            stored += 1

        if stored:
            return f"Recorded {stored} finding(s) for {path}. Do not report them again."
        return f"Recorded {path} as reviewed with no findings."

    def as_all_comments(self) -> List[Tuple[str, list]]:
        """Render as the ``[(file_path, issues), ...]`` shape the pipeline consumes.

        Same contract as the graph path's ``all_comments``, so
        ``extract_issues_for_review`` and everything after it are untouched by
        which review mode produced the findings.
        """
        return [(path, self.findings.get(path, [])) for path in self.reviewed]

    def describe(self) -> str:
        parts = [f"{len(self.reviewed)} file(s) reviewed", f"{self.total} finding(s)"]
        if self.over_cap:
            parts.append(f"{self.over_cap} over cap")
        if self.rejected:
            parts.append(f"{self.rejected} rejected")
        return ", ".join(parts)


def make_record_findings_tool(
    sink: FindingsSink, on_record: Optional[Callable[[str], None]] = None
) -> BaseTool:
    """Build the ``record_findings`` tool bound to ``sink``.

    Built per run, because the sink is per run. The description is written for the
    model: it states the empty-list contract explicitly, since "call me even when
    you found nothing" is the part a model will otherwise optimise away, and that
    is the signal the coverage check depends on.

    ``on_record`` is called with the normalised path after every accepted call. The
    sink keys on the *file*, which cannot say which reviewer reported — and a Go
    service file has two. The review pass builds one tool per reviewer-file pair
    with a callback that marks that pair, which is what makes per-pair completion
    observable for the progress record.
    """

    def _record(file_path: str, findings: Optional[List[RecordedFinding]] = None) -> str:
        parsed: List[RecordedFinding] = []
        for item in findings or []:
            if isinstance(item, RecordedFinding):
                parsed.append(item)
            elif isinstance(item, dict):
                try:
                    parsed.append(RecordedFinding(**item))
                except Exception:
                    sink.rejected += 1
            else:
                sink.rejected += 1
        message = sink.record(file_path, parsed)
        reported = FindingsSink._normalise(file_path)
        if on_record is not None and reported:
            on_record(reported)
        return message

    return StructuredTool.from_function(
        func=_record,
        name="record_findings",
        description=(
            "Report the result of reviewing one file. This is the ONLY way a finding "
            "reaches the merge request — anything you write in prose instead is "
            "discarded. Call it exactly once for the file you were asked to review, "
            "as the last thing you do. Pass findings=[] when the file is clean; that "
            "is a normal, expected result and it records the file as reviewed."
        ),
        args_schema=RecordFindingsArgs,
    )
