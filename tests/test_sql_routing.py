"""The one routing judgement in the system.

An extension identifies every file type here except embedded SQL, so a `.go` file
carrying a hand-built query is the single case static classification cannot see.
The split is deliberate: evidence is gathered by regex (something code does
perfectly), and only substance is judged by a model.
"""

import core.agents.route_classifier as rc
from core.agents.deep_reviewer import add_sql_dispatches, build_dispatch_plan, go_files
from core.agents.route_classifier import (
    MAX_EVIDENCE_LINES, build_router_input, classify_sql_involvement, gather_sql_evidence,
)
from dto.general import SqlRoutingResponse

REAL_QUERY = '''package prc

func (s *Svc) Report(ctx context.Context, f Filter) ([]Row, error) {
\tq := "SELECT o.id, o.total FROM orders o JOIN customers c ON c.id = o.customer_id"
\tif f.Since != "" {
\t\tq += " WHERE o.created_at > '" + f.Since + "'"
\t}
\treturn s.db.QueryContext(ctx, q)
}
'''
INCIDENTAL = '''package prc

// selectBranch picks a branch. We do not update anything here.
func selectBranch(x int) int { return x }
'''
NO_SQL = "package prc\n\nfunc Add(a, b int) int { return a + b }\n"


def _tree(tmp_path):
    (tmp_path / "prc").mkdir()
    (tmp_path / "prc" / "report.go").write_text(REAL_QUERY)
    (tmp_path / "prc" / "plain.go").write_text(INCIDENTAL)
    (tmp_path / "prc" / "none.go").write_text(NO_SQL)
    return tmp_path


def test_evidence_finds_queries_and_db_calls(tmp_path):
    root = _tree(tmp_path)
    hits = gather_sql_evidence("prc/report.go", str(root))
    assert hits, "a real query must produce evidence"
    assert any("SELECT o.id" in text for _n, text in hits)
    assert any("QueryContext" in text for _n, text in hits)


def test_a_file_with_no_sql_produces_no_evidence(tmp_path):
    """No evidence means the file never reaches an LLM at all."""
    root = _tree(tmp_path)
    assert gather_sql_evidence("prc/none.go", str(root)) == []


def test_a_missing_file_is_not_an_error(tmp_path):
    assert gather_sql_evidence("prc/gone.go", str(tmp_path)) == []


def test_evidence_is_capped_per_file(tmp_path):
    (tmp_path / "big.go").write_text(
        "package p\n" + "\n".join(f'q{i} := "SELECT {i}"' for i in range(50)))
    assert len(gather_sql_evidence("big.go", str(tmp_path))) == MAX_EVIDENCE_LINES


def test_payload_carries_paths_and_matched_lines(tmp_path):
    root = _tree(tmp_path)
    payload = build_router_input({"prc/report.go": gather_sql_evidence("prc/report.go", str(root))})
    assert "[prc/report.go]" in payload and "SELECT o.id" in payload


class _Fake:
    def __init__(self, response=None, boom=False):
        self.response, self.boom, self.seen = response, boom, None

    async def run(self, payload, config=None):
        self.seen = payload
        if self.boom:
            raise RuntimeError("provider said no")
        return self.response


def _run(coro):
    import asyncio
    return asyncio.run(coro)


def test_only_scanned_paths_are_honoured(monkeypatch, tmp_path):
    """A model naming a path we never scanned is hallucinating or renaming."""
    root = _tree(tmp_path)
    agent = _Fake(SqlRoutingResponse(decisions=[
        {"file_path": "prc/report.go", "needs_sql_review": "yes", "reason": "runtime WHERE"},
        {"file_path": "prc/plain.go", "needs_sql_review": False, "reason": "comment only"},
        {"file_path": "prc/never_scanned.go", "needs_sql_review": True, "reason": "invented"},
    ]))
    monkeypatch.setattr(rc, "create_sql_router_agent", lambda *a, **k: agent)
    paths, err = _run(classify_sql_involvement(
        ["prc/report.go", "prc/plain.go", "prc/none.go"], str(root)))
    assert err is None
    assert paths == ["prc/report.go"]
    assert "prc/none.go" not in agent.seen, "a signal-free file must not enter the prompt"


def test_no_signals_means_no_llm_call(monkeypatch, tmp_path):
    root = _tree(tmp_path)
    called = []

    class Boom(_Fake):
        async def run(self, payload, config=None):
            called.append(1)
            raise AssertionError("must not be called")

    monkeypatch.setattr(rc, "create_sql_router_agent", lambda *a, **k: Boom())
    paths, err = _run(classify_sql_involvement(["prc/none.go"], str(root)))
    assert paths == [] and err is None and not called


def test_fails_closed_on_an_agent_error(monkeypatch, tmp_path):
    """A spurious reviewer costs a whole pass; a missed one costs a second opinion."""
    root = _tree(tmp_path)
    monkeypatch.setattr(rc, "create_sql_router_agent", lambda *a, **k: _Fake(boom=True))
    paths, err = _run(classify_sql_involvement(["prc/report.go"], str(root)))
    assert paths == [] and err and "failed" in err


def test_fails_closed_when_structured_output_is_none(monkeypatch, tmp_path):
    root = _tree(tmp_path)
    monkeypatch.setattr(rc, "create_sql_router_agent", lambda *a, **k: _Fake(None))
    paths, err = _run(classify_sql_involvement(["prc/report.go"], str(root)))
    assert paths == [] and err


def test_only_service_files_are_candidates():
    classified = [("prc/report.go", "service"), ("prc/db.go", "sql"), ("ui/a.ts", "typescript")]
    assert go_files(classified) == ["prc/report.go"], "db.go is already the sql reviewer's"


def test_additions_never_displace_the_static_assignment():
    classified = [("prc/report.go", "service"), ("prc/db.go", "sql")]
    plan = add_sql_dispatches(build_dispatch_plan(classified), ["prc/report.go"])
    pairs = set(plan)
    assert ("prc/report.go", "service_reviewer") in pairs
    assert ("prc/report.go", "service_business_reviewer") in pairs
    assert ("prc/report.go", "sql_reviewer") in pairs, "additive, not a swap"


def test_db_go_is_not_dispatched_twice():
    classified = [("prc/db.go", "sql")]
    plan = add_sql_dispatches(build_dispatch_plan(classified), ["prc/db.go"])
    assert sum(1 for p, n in plan if (p, n) == ("prc/db.go", "sql_reviewer")) == 1
