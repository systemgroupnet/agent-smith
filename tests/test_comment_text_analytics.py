"""What a posted comment *said* reaches the collector.

``review_comments`` used to store where a comment was (file, line, severity,
model) and never its text, so a human reply in ``comment_replies`` could be read
but not what it answered. These tests cover both ends of the fix: the reviewer
puts the finding's text and file type into each ``posted_comments`` entry, and the
collector stores them leniently — an older event without them, or garbage in
them, lands as NULL and never costs the event or a previously stored text.

No database: the collector's row builder is pure, and its upsert is checked by
compiling the statement against the PostgreSQL dialect.
"""

import ast
import importlib.util
from pathlib import Path

from sqlalchemy.dialects import postgresql

import storage
from dto.analytics import (
    ANALYTICS_SCHEMA_VERSION,
    RunMetrics,
    _MAX_PROBLEM_CHARS,
    posted_comment_entry,
)

REPO = Path(__file__).resolve().parent.parent
POSTED = {"note_id": 991, "discussion_id": "abc123", "comment_type": "inline"}


def _entry(**overrides):
    kwargs = dict(
        file="svc/order.go",
        line=42,
        severity="high",
        model="glm-5.3",
        problem="`Save()` ignores the error from `tx.Commit()`.",
        file_type="service",
    )
    kwargs.update(overrides)
    return posted_comment_entry(POSTED, **kwargs)


# --- reviewer: the posted_comments entry --------------------------------------


def test_entry_carries_the_finding_text_and_file_type():
    entry = _entry()
    assert entry == {
        "note_id": 991,
        "discussion_id": "abc123",
        "comment_type": "inline",
        "file": "svc/order.go",
        "line": 42,
        "severity": "high",
        "model": "glm-5.3",
        "problem": "`Save()` ignores the error from `tx.Commit()`.",
        "file_type": "service",
    }


def test_entry_text_is_clipped_so_one_runaway_finding_cannot_bloat_the_event():
    entry = _entry(problem="x" * (_MAX_PROBLEM_CHARS + 500))
    assert len(entry["problem"]) == _MAX_PROBLEM_CHARS == 2000


def test_blank_or_missing_text_is_none_rather_than_an_empty_string():
    for value in (None, "", "   \n "):
        assert _entry(problem=value)["problem"] is None
    assert _entry(file_type="")["file_type"] is None
    assert _entry(file_type=None)["file_type"] is None


def test_nul_bytes_are_removed_because_postgres_rejects_them_in_jsonb():
    assert _entry(problem="bad\x00byte")["problem"] == "badbyte"


def test_entry_survives_serialization_into_the_run_event_without_a_schema_bump():
    metrics = RunMetrics()
    metrics.posted_comments.append(_entry())
    dumped = metrics.model_dump(mode="json")
    assert dumped["posted_comments"][0]["problem"].startswith("`Save()`")
    assert dumped["posted_comments"][0]["file_type"] == "service"
    # Additive, optional fields: the module's own rule says no bump.
    assert dumped["schema_version"] == ANALYTICS_SCHEMA_VERSION == "1"


def _posting_call() -> ast.Call:
    """The ``posted_comment_entry(...)`` call in reviewer/main.py's posting loop.

    main.py can't be imported here (it shares its module name with the
    collector's, and pulls in the whole runtime), so the call site is read as
    syntax — enough to pin which fields the loop hands over.
    """
    tree = ast.parse((REPO / "reviewer" / "main.py").read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "posted_comment_entry"
    ]
    assert len(calls) == 1, "posted_comments entries must be built in one place"
    return calls[0]


def test_the_posting_loop_hands_over_the_finding_text_and_profile_type():
    call = _posting_call()
    keywords = {kw.arg: ast.unparse(kw.value) for kw in call.keywords}
    assert {"file", "line", "severity", "model", "problem", "file_type"} <= set(keywords)
    # The finding's own field, never the rendered comment body (whose suggestion
    # block is the reviewed repository's source code).
    problem_src = keywords["problem"]
    assert "issue" in problem_src and "'problem'" in problem_src
    assert "comment" not in problem_src
    assert "get_profile().classify(" in keywords["file_type"]


# --- collector: row building --------------------------------------------------


def _row(entry):
    return storage._comment_row(
        entry,
        received_at="2026-10-11T00:00:00+00:00",
        run_id="run-1",
        review_run_id=7,
        project_path="group/project",
        mr_id="123",
        mr_author="alice",
    )


def test_collector_row_maps_the_new_fields():
    row = _row(_entry())
    assert row["problem"] == "`Save()` ignores the error from `tx.Commit()`."
    assert row["file_type"] == "service"
    assert row["note_id"] == 991
    assert row["severity"] == "high"


def test_an_event_from_an_older_reviewer_stores_null_for_the_new_fields():
    legacy = {k: v for k, v in _entry().items() if k not in ("problem", "file_type")}
    row = _row(legacy)
    assert row is not None
    assert row["problem"] is None and row["file_type"] is None


def test_garbage_in_the_new_fields_is_null_never_a_dropped_comment():
    for garbage in ({"text": "hi"}, ["a"], 12, 3.5, True, "   "):
        row = _row({**_entry(), "problem": garbage, "file_type": garbage})
        assert row is not None, garbage
        assert row["problem"] is None, garbage
        assert row["file_type"] is None, garbage


def test_only_an_entry_with_no_usable_note_id_is_skipped():
    assert _row("not a dict") is None
    assert _row({**_entry(), "note_id": None}) is None
    assert _row({**_entry(), "note_id": "n/a"}) is None
    assert _row({**_entry(), "note_id": "991"})["note_id"] == 991


def test_a_resent_event_without_the_text_cannot_erase_the_stored_text():
    stmt = _upsert_sql([_row(_entry())])
    assert "coalesce(excluded.problem, review_comments.problem)" in stmt
    assert "coalesce(excluded.file_type, review_comments.file_type)" in stmt
    # The positional fields still follow the latest event, as before.
    assert "severity = excluded.severity" in stmt


def _upsert_sql(rows) -> str:
    compiled = storage._upsert_review_comments(rows).compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).split()).lower()


def test_the_table_definition_has_the_new_columns():
    cols = storage.review_comments.c
    assert "problem" in cols and "file_type" in cols
    assert cols.problem.nullable and cols.file_type.nullable


# --- collector: the migration -------------------------------------------------


VERSIONS = REPO / "collector" / "migrations" / "versions"


def _load_migration(path: Path):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_0013_adds_both_columns_on_top_of_0012():
    module = _load_migration(VERSIONS / "0013_review_comment_text.py")
    assert module.revision == "0013"
    assert module.down_revision == "0012"
    added = {(table, column) for table, column, _ in module._ADDITIONS}
    assert added == {("review_comments", "problem"), ("review_comments", "file_type")}
    # The migration and the table definition must agree on what exists.
    for table, column, make in module._ADDITIONS:
        assert column in storage.metadata.tables[table].c
        assert make().name == column


def test_the_revision_chain_is_linear_with_one_head():
    modules = [_load_migration(p) for p in sorted(VERSIONS.glob("0*.py"))]
    revisions = {m.revision: m.down_revision for m in modules}
    heads = set(revisions) - {down for down in revisions.values() if down}
    assert heads == {"0013"}
    # Walk back from the head to the root: every revision is visited once.
    seen, current = [], "0013"
    while current:
        seen.append(current)
        current = revisions[current]
    assert sorted(seen) == sorted(revisions)
