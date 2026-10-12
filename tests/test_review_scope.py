"""What a reviewer reviews, and what a high has to survive before it is recorded.

From an analysis of 496 developer replies to the agent's comments: ~16% rejected a
comment as clashing with a team convention, ~15% as factually wrong (library
semantics stated from memory; "X is not validated" when the check lives one call
away), ~7% as unreachable yet badged high, ~6% as out of scope (pre-existing code,
code only moved into an `if`, a rename). Two shared rules answer that —
``CHANGE_SCOPE_RULE`` and ``VERIFY_BEFORE_REPORTING`` — and the ranking stage gets
the matching demotion and the project's house rules.

Prompt *wording* is asserted here; model behaviour is not.
"""

import textwrap

import pytest

from core.agents.review_contexts import ReviewPromptContext
from core.prompts import (
    get_business_logic_reviewer_prompt, get_comment_reviewer_prompt,
    get_dataview_reviewer_prompt, get_e2e_reviewer_prompt, get_html_reviewer_prompt,
    get_pbook_reviewer_prompt,
    get_scss_reviewer_prompt, get_semantic_reviewer_prompt, get_sql_reviewer_prompt,
    get_test_reviewer_prompt, get_typescript_reviewer_prompt,
)
from core.prompts.common import (
    CHANGE_SCOPE_RULE, EMPTY_FINDING_RULE, NO_COMPILE_ERRORS_RULE, PLAIN_LANGUAGE_RULE,
    SEVERITY_RUBRIC, SUGGESTION_FIELD_RULE, SYMBOL_FORMATTING_RULE,
    VERIFY_BEFORE_REPORTING,
)
from core.prompts.deep_prompts import format_checkout_addendum

REVIEWER_PROMPTS = {
    "semantic": get_semantic_reviewer_prompt,
    "business": get_business_logic_reviewer_prompt,
    "sql": get_sql_reviewer_prompt,
    "test": get_test_reviewer_prompt,
    "e2e": get_e2e_reviewer_prompt,
    "typescript": get_typescript_reviewer_prompt,
    "html": get_html_reviewer_prompt,
    "scss": get_scss_reviewer_prompt,
    "pbook": get_pbook_reviewer_prompt,
    "dataview": get_dataview_reviewer_prompt,
}

#: Every shared rule a reviewer prompt interpolates, EMPTY_FINDING_RULE excluded —
#: it is the pre-submission check and has to come after all of these.
SHARED_RULES = {
    "NO_COMPILE_ERRORS_RULE": NO_COMPILE_ERRORS_RULE,
    "SEVERITY_RUBRIC": SEVERITY_RUBRIC,
    "CHANGE_SCOPE_RULE": CHANGE_SCOPE_RULE,
    "VERIFY_BEFORE_REPORTING": VERIFY_BEFORE_REPORTING,
    "SUGGESTION_FIELD_RULE": SUGGESTION_FIELD_RULE,
    "SYMBOL_FORMATTING_RULE": SYMBOL_FORMATTING_RULE,
    "PLAIN_LANGUAGE_RULE": PLAIN_LANGUAGE_RULE,
}


def _render(name):
    return REVIEWER_PROMPTS[name](ReviewPromptContext(), "")


# ── the two new shared rules: everywhere, once, in order ─────────────────────

@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_every_reviewer_carries_both_new_rules_exactly_once(name):
    text = _render(name)
    assert text.count(CHANGE_SCOPE_RULE) == 1
    assert text.count(VERIFY_BEFORE_REPORTING) == 1


@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_the_new_rules_follow_the_rubric_and_the_empty_finding_rule_stays_last(name):
    text = _render(name)
    rubric = text.index(SEVERITY_RUBRIC)
    assert text.index(CHANGE_SCOPE_RULE) > rubric
    assert text.index(VERIFY_BEFORE_REPORTING) > rubric
    last = text.index(EMPTY_FINDING_RULE)
    for rule_name, rule in SHARED_RULES.items():
        assert text.index(rule) < last, f"{rule_name} must come before EMPTY_FINDING_RULE"


def test_the_scope_rule_says_what_is_not_new_code():
    assert "only re-indented (for example wrapped in a new `if`)" in CHANGE_SCOPE_RULE
    assert "changed only by a rename is not new code" in CHANGE_SCOPE_RULE
    assert "makes it reachable on a new path, or makes it worse" in CHANGE_SCOPE_RULE
    assert "\"Pre-existing, not changed by this MR:\" and never mark it high" in CHANGE_SCOPE_RULE
    assert "If the Git Diff section is empty, treat the whole file as changed." in CHANGE_SCOPE_RULE


def test_every_verification_check_has_an_exit_other_than_dropping():
    """Without "or record it as medium" the rule reads as "report nothing you
    cannot prove", which is the opposite failure."""
    rule = VERIFY_BEFORE_REPORTING
    assert "drop the finding or record it as medium" in rule
    assert "House pattern" in rule and "appears in N places" in rule
    assert "Name what you read in `problem`" in rule
    assert "provable from the repository" in rule and "phrased as a question" in rule
    assert "No trigger means medium." in rule


@pytest.mark.parametrize("name", sorted(REVIEWER_PROMPTS))
def test_no_prompt_still_carries_its_own_scope_line(name):
    """The per-prompt lines contradicted the shared rule ("review the whole file")
    or duplicated it; each now defers to it."""
    text = _render(name)
    assert "Focus on changed code (Git Diff)" not in text
    assert "In a new spec, review the whole file." not in text


# ── plain language: the example is the strongest instruction ────────────────

def test_the_plain_language_example_carries_a_trigger():
    """The old example was the hypothetical developers rejected most often."""
    assert "If `order` is nil" not in PLAIN_LANGUAGE_RULE
    assert "returns `nil, nil` for an unknown id" in PLAIN_LANGUAGE_RULE


def test_the_plain_language_rule_bans_escalation_and_explaining():
    assert "State one consequence, the most direct one." in PLAIN_LANGUAGE_RULE
    assert "\"catastrophic\"" in PLAIN_LANGUAGE_RULE
    assert "The reader wrote this code and knows its context." in PLAIN_LANGUAGE_RULE
    assert "Simple means easy to read, not less precise." in PLAIN_LANGUAGE_RULE


# ── the checkout addendum ────────────────────────────────────────────────────

def test_the_addendum_points_at_the_changes_and_widens_the_absence_check():
    addendum = format_checkout_addendum(clone_root="/x", agent_type="service",
                                        source_branch="feature/x")
    assert "Review that file.\n" not in addendum
    assert "Review that file's changes" in addendum
    assert "not validated, a caller is not authorized" in addendum
    assert "VERIFY A HIGH BEFORE YOU RECORD IT" in addendum
    assert "VERIFY A HIGH BEFORE YOU RECORD IT" in VERIFY_BEFORE_REPORTING, \
        "the addendum names the rule by its heading"


# ── SQL ──────────────────────────────────────────────────────────────────────

def test_the_sql_reviewer_reads_the_whole_migration_history():
    text = _render("sql")
    assert "`ALTER TABLE <name>` across all migration directories" in text
    assert "(`v5/`, `v6/`, `v7/`)" in text


def test_the_generic_sql_prompt_keeps_uniqueness_for_the_profile_to_override():
    """Whether uniqueness belongs in the database is a house rule, and house rules
    live in the profile: one team enforces it in the application, another uses
    UNIQUE throughout. The generic prompt stays generic; a profile that disagrees
    says so under Additional Instructions, which outrank it."""
    assert "Missing uniqueness" in _render("sql")


# ── the comment reviewer ─────────────────────────────────────────────────────

def test_the_comment_reviewer_renders_house_rules():
    text = get_comment_reviewer_prompt(instructions="We never use foreign keys.")
    assert "=== Additional Instructions ===\nWe never use foreign keys." in text
    assert "Against This Project's House Rules" in text


def test_the_comment_reviewer_without_house_rules_says_so():
    text = get_comment_reviewer_prompt()
    assert "=== Additional Instructions ===\nNo additional instructions defined for this agent." in text


class _StubProfile:
    def __init__(self):
        self.asked = []

    def instructions_for(self, key):
        self.asked.append(key)
        return "Quote freely: `if err != nil { return err }`."


def _comment_reviewer(monkeypatch, profile):
    import core.agents.comment_reviewer_agent as cra
    monkeypatch.setattr(cra, "get_profile", lambda: profile)
    return cra.CommentReviewerAgent()


def test_the_comment_reviewer_asks_the_profile_for_its_own_key(monkeypatch):
    profile = _StubProfile()
    agent = _comment_reviewer(monkeypatch, profile)
    assert profile.asked == ["comment_reviewer"]
    assert "Quote freely" in agent.get_system_prompt()


def test_a_brace_in_house_rules_does_not_break_the_ranking_call(monkeypatch):
    """As a ("system", text) template, `{ return err }` is a variable nobody
    supplies — every call would fail and the stage would fall back every run."""
    from langchain_core.runnables import RunnableLambda

    agent = _comment_reviewer(monkeypatch, _StubProfile())

    class _Model:
        def with_structured_output(self, *a, **k):
            return RunnableLambda(lambda prompt_value: prompt_value)

    rendered = agent._build_chain(_Model()).invoke({"input": "[]"})
    system = rendered.to_messages()[0].content
    assert "`if err != nil { return err }`" in system


def test_a_profile_that_cannot_load_costs_the_house_rules_not_the_stage(monkeypatch):
    import core.agents.comment_reviewer_agent as cra
    from core.config.review_profile import ProfileError

    def boom():
        raise ProfileError("cannot read review profile /nope.yml")

    monkeypatch.setattr(cra, "get_profile", boom)
    assert cra.profile_instructions() == ""
    assert "No additional instructions defined" in cra.CommentReviewerAgent().get_system_prompt()


def test_the_profile_all_text_reaches_the_comment_reviewer(monkeypatch, tmp_path):
    """End to end through a real profile file: ``all`` applies to this stage too."""
    import core.agents.comment_reviewer_agent as cra
    from core.config.review_profile import reset_profile

    path = tmp_path / "p.yml"
    path.write_text(textwrap.dedent("""\
        version: 1
        file_types:
          - name: service
            match: ["*.go"]
            reviewers: [service_reviewer]
        instructions:
          all: Uniqueness is enforced in the application, not the database.
    """))
    monkeypatch.setenv("REVIEW_PROFILE", str(path))
    reset_profile()
    assert "Uniqueness is enforced in the application" in \
        cra.CommentReviewerAgent().get_system_prompt()


# ── a reviewer cut short ─────────────────────────────────────────────────────

def test_a_salvaged_reviewer_caps_what_it_did_not_verify():
    from core.agents.deep_reviewer import REPORT_NOW_INSTRUCTION
    assert "as medium at most, never high" in REPORT_NOW_INSTRUCTION
    assert "`record_findings`" in REPORT_NOW_INSTRUCTION
