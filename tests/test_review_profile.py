"""The review profile: file classification and routing as YAML, not code."""

import textwrap

import pytest
import yaml

from core.agents.deep_reviewer import REVIEWER_SPECS, build_dispatch_plan, go_files, route_file
from core.config.review_profile import (
    COMMENT_REVIEWER, PROFILES_DIR, ProfileError, get_profile, load_profile, parse_profile,
    reset_profile,
)
from core.utils.file_classifier import StaticFileClassifier

KNOWN = [s.name for s in REVIEWER_SPECS]


def _use(monkeypatch, name):
    monkeypatch.setenv("REVIEW_PROFILE", name)
    reset_profile()


def test_the_default_profile_has_no_company_conventions():
    profile = get_profile()
    assert profile.source.endswith("default.yml")
    assert profile.classify("prc/pbook.go") == "service"
    assert profile.classify("prc/dataview.go") == "service"
    assert profile.classify("prc/ts.go") == "service"
    assert profile.classify("prc/db.go") == "service"
    assert profile.classify("web/app.tsx") == "typescript"
    assert profile.classify("x/y_test.go") == "test"


def test_only_an_e2e_spec_goes_to_the_e2e_reviewer():
    """A unit test is a *.spec.ts too, so the default rule is scoped by path."""
    profile = get_profile()
    assert profile.classify("e2e/login.spec.ts") == "e2e"
    assert profile.classify("apps/web-e2e/src/checkout/pay.spec.ts") == "e2e"
    assert profile.classify("src/app/cart.component.spec.ts") == "typescript"
    assert profile.classify("e2e/support/login.page.ts") == "typescript"
    assert route_file("e2e") == ("e2e_reviewer",)


def test_the_house_rules_go_to_the_rules_reviewer():
    """Root and module `.aid/*.md`; any other Markdown stays unreviewed."""
    profile = get_profile()
    assert profile.classify(".aid/service.md") == "aid"
    assert profile.classify("billing/.aid/sql.md") == "aid"
    assert profile.classify("docs/guide.md") == "unknown"
    assert route_file("aid") == ("rules_reviewer",)


def test_the_rules_reviewer_knows_every_rules_file_name():
    """Its list of names is a copy of REVIEWER_SPECS: a reviewer added without it
    would have its rules file reported as read by nobody."""
    from core.agents.review_contexts import ReviewPromptContext
    from core.prompts import get_rules_reviewer_prompt

    prompt = get_rules_reviewer_prompt(ReviewPromptContext())
    for agent_type in {s.agent_type for s in REVIEWER_SPECS}:
        assert f"`{agent_type}.md`" in prompt, agent_type


def test_every_bundled_profile_loads():
    names = sorted(p.stem for p in PROFILES_DIR.glob("*.yml"))
    assert "default" in names
    for name in names:
        load_profile(name, KNOWN)


def test_routing_comes_from_the_profile():
    assert route_file("service") == ("service_reviewer", "service_business_reviewer")
    assert route_file("sql") == ("sql_reviewer",)
    assert route_file("unknown") == ()
    plan = build_dispatch_plan([("a.go", "service"), ("README.md", "unknown")])
    assert plan == [("a.go", "service_reviewer"), ("a.go", "service_business_reviewer")]


def test_business_logic_switch_still_applies(monkeypatch):
    monkeypatch.setenv("BUSINESS_LOGIC_REVIEW_ENABLED", "false")
    assert route_file("service") == ("service_reviewer",)


def test_sql_scan_types_come_from_the_profile(tmp_path, monkeypatch):
    path = tmp_path / "p.yml"
    path.write_text(textwrap.dedent("""
        version: 1
        file_types:
          - {name: backend, match: ["*.py"], reviewers: [service_reviewer]}
          - {name: sql, match: ["*.sql"], reviewers: [sql_reviewer]}
        sql_routing: {scan_file_types: [backend]}
    """))
    _use(monkeypatch, str(path))
    assert go_files([("a.py", "backend"), ("b.sql", "sql")]) == ["a.py"]


def test_a_path_pattern_matches_the_whole_path():
    profile = parse_profile({
        "file_types": [
            {"name": "sql", "match": ["migrations/*"], "reviewers": ["sql_reviewer"]},
            {"name": "service", "match": ["*.go"], "reviewers": ["service_reviewer"]},
        ]}, "t", KNOWN)
    assert profile.classify("migrations/001.go") == "sql"
    assert profile.classify("/migrations/001.go") == "sql"
    assert profile.classify("svc/migrations.go") == "service"


def test_first_match_wins():
    profile = parse_profile({
        "file_types": [
            {"name": "service", "match": ["*.go"], "reviewers": ["service_reviewer"]},
            {"name": "sql", "match": ["db.go"], "reviewers": ["sql_reviewer"]},
        ]}, "t", KNOWN)
    assert profile.classify("db.go") == "service", "order is the documented tie-break"


@pytest.mark.parametrize("doc,needle", [
    ({"file_types": [{"name": "x", "match": ["*.x"], "reviewers": ["nope"]}]}, "unknown reviewer"),
    ({"file_types": [{"name": "x", "match": [], "reviewers": ["sql_reviewer"]}]}, "no 'match'"),
    ({"file_types": [{"name": "x", "match": ["*.x"], "reviewers": []}]}, "names no reviewers"),
    ({"file_types": [{"name": "unknown", "match": ["*"], "reviewers": ["sql_reviewer"]}]}, "needs a 'name'"),
    ({"file_types": []}, "non-empty list"),
    ({"version": 2, "file_types": []}, "unsupported profile version"),
    ({"file_types": [{"name": "x", "match": ["*.x"], "reviewers": ["sql_reviewer"]}],
      "sql_routing": {"scan_file_types": ["service"]}}, "undeclared file type"),
    ({"file_types": [
        {"name": "x", "match": ["*.x"], "reviewers": ["sql_reviewer"]},
        {"name": "x", "match": ["*.y"], "reviewers": ["test_reviewer"]}]}, "two different reviewer sets"),
])
def test_a_broken_profile_is_rejected_at_load_time(doc, needle):
    with pytest.raises(ProfileError, match=needle):
        parse_profile(doc, "t", KNOWN)


def test_a_missing_profile_file_is_a_profile_error(monkeypatch, tmp_path):
    _use(monkeypatch, str(tmp_path / "missing.yml"))
    with pytest.raises(ProfileError, match="cannot read"):
        get_profile()


def test_the_bundled_profiles_are_valid_yaml_documents():
    for path in PROFILES_DIR.glob("*.yml"):
        assert isinstance(yaml.safe_load(path.read_text()), dict), path


def test_instructions_reach_the_reviewer_prompt(tmp_path, monkeypatch):
    path = tmp_path / "p.yml"
    path.write_text(textwrap.dedent("""
        version: 1
        file_types:
          - {name: sql, match: ["*.sql"], reviewers: [sql_reviewer]}
        instructions:
          all: EVERYONE-RULE
          sql_reviewer: SQL-ONLY-RULE
    """))
    _use(monkeypatch, str(path))
    profile = get_profile()
    assert profile.instructions_for("sql_reviewer") == "EVERYONE-RULE\n\nSQL-ONLY-RULE"
    assert profile.instructions_for("test_reviewer") == "EVERYONE-RULE"


@pytest.mark.parametrize("key", ["sql_reviwer", "comment_reviewers", "comments", "ranker"])
def test_instructions_for_an_unknown_reviewer_are_rejected(key):
    with pytest.raises(ProfileError, match="instructions for unknown reviewer") as err:
        parse_profile({
            "file_types": [{"name": "x", "match": ["*.x"], "reviewers": ["sql_reviewer"]}],
            "instructions": {key: "typo"},
        }, "t", KNOWN)
    assert repr(COMMENT_REVIEWER) in str(err.value), "the error lists every accepted key"


def test_the_comment_reviewer_takes_instructions(tmp_path, monkeypatch):
    """`comment_reviewer` is the ranking stage, not a reviewer: a profile teaches it
    which findings contradict a house convention, so it can drop them before posting."""
    path = tmp_path / "p.yml"
    path.write_text(textwrap.dedent("""
        version: 1
        file_types:
          - {name: sql, match: ["*.sql"], reviewers: [sql_reviewer]}
        instructions:
          all: EVERYONE-RULE
          comment_reviewer: RANKING-RULE
    """))
    _use(monkeypatch, str(path))
    profile = get_profile()
    assert COMMENT_REVIEWER == "comment_reviewer"
    assert profile.instructions_for(COMMENT_REVIEWER) == "EVERYONE-RULE\n\nRANKING-RULE"
    assert profile.instructions_for("sql_reviewer") == "EVERYONE-RULE"


def test_no_file_type_can_be_routed_to_the_comment_reviewer():
    assert COMMENT_REVIEWER not in KNOWN
    with pytest.raises(ProfileError, match="unknown reviewer"):
        parse_profile({
            "file_types": [{"name": "x", "match": ["*.x"], "reviewers": [COMMENT_REVIEWER]}],
        }, "t", KNOWN)


def test_the_bundled_prompts_carry_no_house_rules():
    """Company policy belongs in a profile, never in the shared prompts."""
    from core.agents.review_contexts import ReviewPromptContext

    for spec in REVIEWER_SPECS:
        prompt = spec.prompt_getter(ReviewPromptContext(), "").lower()
        for term in ("foreign key", "persian", "godog", "gorm", "angular", "erp system"):
            assert term not in prompt, f"{spec.name} prompt still says {term!r}"
