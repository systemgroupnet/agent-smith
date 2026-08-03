"""Review verbosity — how many findings, and of which severities, reach the MR.

By default the reviewer is deliberately terse: only **high**-severity findings are
considered at all, and the comment reviewer ranks those down to ~10. That is the
right default (a wall of comments gets scrolled past), but it hides two things a
human sometimes wants: a genuine high that lost a slot, and the medium findings,
which today are counted into analytics and then **discarded** without ever
reaching the in-diff gate.

A **GitLab MR label** raises the level, so it is per-MR and needs no pipeline
config — add the label, re-run the pipeline, get more. The reviewer already reads
``mr_data["labels"]`` and writes labels of its own (``ai-reviewed``,
``review effort::N/5``), so this rides on plumbing that exists.

    (no label)       high only, ranked down to ~10          ← unchanged default
    review::-v       every high that survives the gate
    review::-vv      every high, plus the mediums worth posting
    review::-vvv     every high and every medium, capped at 30

``review::`` is GitLab's **scoped-label** syntax (``key::value``), which is what
these are in the project. GitLab enforces one label per scope, so adding
``review::-vvv`` to an MR already carrying ``review::-v`` *replaces* it rather
than stacking — the level is unambiguous by construction. That exclusivity is a
Premium behavior though, and on Free tier ``key::value`` is just a label name with
no enforcement, so :func:`parse_verbosity` still resolves conflicts itself
(loudest wins) rather than trusting there to be only one.

What verbosity does **not** touch
---------------------------------
The compile-time purge. Those findings are *false* — the MR's code was built and
run before the MR was opened — so they are not "less important findings" to be
unlocked at a higher level; they are wrong at every level. Same for the style /
documentation / no-rationale filters: raising verbosity lifts the *budget*, it
does not turn the comment reviewer into a pass-through. ``-vvv`` is the closest to
that, and even it purges.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional, Tuple

#: Canonical label prefix: GitLab scoped-label syntax, ``review::-vv``. This is the
#: form reported in logs and in ``RunMetrics.review_verbosity``.
LABEL_PREFIX = "review::"

#: Other prefixes accepted on the way in. A single colon is the easy typo (and was
#: this feature's first shape), and a bare ``-vv`` is what the flag looks like, so
#: someone will type it. Recognising them costs nothing and the alternative is a
#: label that looks applied and silently does nothing.
_ACCEPTED_PREFIXES = (LABEL_PREFIX, "review:", "")

#: Ceiling at the top level. "All highs and mediums" can be 60+ comments on a
#: large MR, which buries the author and costs ~6 GitLab calls each (the post plus
#: five rating reactions). Truncation here is always logged and recorded, never
#: silent — see ``main.py``'s ``comment_over_cap`` warning.
MAX_COMMENTS_VVV = 30

#: The default budget, i.e. the "TOP 10" the comment reviewer prompt has always used.
DEFAULT_MAX_COMMENTS = 10


@dataclass(frozen=True)
class Verbosity:
    """One verbosity level, and everything downstream needs to know from it."""

    level: int
    label: str  # "" for the default
    severities: Tuple[str, ...]  # which severities reach the gate at all
    max_comments: int
    #: True when the comment reviewer should *rank and select*; False when it
    #: should keep everything that survives its purge and filters.
    rank: bool

    @property
    def name(self) -> str:
        return self.label or "default"

    def describe(self) -> str:
        sev = "+".join(self.severities)
        mode = "ranked" if self.rank else "keep all that qualify"
        return f"{self.name} (severities: {sev}, cap: {self.max_comments}, {mode})"


DEFAULT = Verbosity(0, "", ("high",), DEFAULT_MAX_COMMENTS, rank=True)

_FLAGS = {1: "-v", 2: "-vv", 3: "-vvv"}

_LEVELS = {
    0: DEFAULT,
    1: Verbosity(1, f"{LABEL_PREFIX}-v", ("high",), MAX_COMMENTS_VVV, rank=False),
    2: Verbosity(2, f"{LABEL_PREFIX}-vv", ("high", "medium"), MAX_COMMENTS_VVV, rank=True),
    3: Verbosity(3, f"{LABEL_PREFIX}-vvv", ("high", "medium"), MAX_COMMENTS_VVV, rank=False),
}

#: Exact label → level. An exact-match dict rather than prefix matching, because
#: ``"review::-vvv".startswith("review::-v")`` is True: any startswith-based scan
#: would match the loudest label as the quietest level unless it happened to test
#: longest-first. Exact keys make the ordering irrelevant.
_LABEL_TO_LEVEL = {
    f"{prefix}{flag}": level
    for level, flag in _FLAGS.items()
    for prefix in _ACCEPTED_PREFIXES
}


#: A ``-v`` run of any length, for the two cases the exact table cannot cover:
#: ``-vvvv`` and longer (clamped up to the loudest level rather than ignored), and
#: recognising a label as "meant to be a verbosity flag" so an unusable one warns.
_FLAG_RE = re.compile(r"^-(v+)$")


def all_labels() -> Tuple[str, ...]:
    """Every recognised label, loudest first. For docs and log lines."""
    return tuple(_LEVELS[lvl].label for lvl in (3, 2, 1))


def parse_verbosity(labels: Optional[Iterable[Any]]) -> Verbosity:
    """Resolve the verbosity level from an MR's labels.

    Accepts ``review::-vv`` (the canonical scoped form), ``review:-vv`` and a bare
    ``-vv``, case-insensitively and whitespace-stripped. GitLab returns labels as a
    list of strings, but some payloads hand back label *objects*, so each entry is
    coerced through its ``name``.

    **The loudest label present wins.** GitLab's scoped labels should make a
    conflict impossible — one label per ``review::`` scope — but that enforcement is
    Premium-only, and a project on Free tier can carry both ``review::-v`` and
    ``review::-vvv`` at once. Someone in that state means "more", and behavior must
    not depend on the order GitLab happened to return the labels in.
    """
    best = 0
    for raw in labels or ():
        name = raw.get("name", "") if isinstance(raw, dict) else raw
        key = str(name or "").strip().lower()
        if not key:
            continue
        level = _LABEL_TO_LEVEL.get(key)
        if level is None:
            level = _lenient_level(key)
        if level and level > best:
            best = level
    return _LEVELS[best]


def _lenient_level(key: str) -> Optional[int]:
    """Resolve a label the exact table missed, for the two cases worth rescuing.

    ``-vvvv`` and longer clamps to the loudest level: someone adding a fourth ``v``
    means "even more", and silently giving them the *quietest* setting — which is
    what an unmatched label does — is the worst possible reading of that intent.

    Anything else inside the ``review::`` scope is a typo, and is reported rather
    than ignored, because a label that looks applied and does nothing is the kind of
    thing nobody notices for weeks.
    """
    for prefix in _ACCEPTED_PREFIXES:
        if not prefix or not key.startswith(prefix):
            continue
        flag = key[len(prefix):]
        match = _FLAG_RE.match(flag)
        if match:
            return min(len(match.group(1)), max(_FLAGS))
        print(
            f"  ⚠️  [verbosity] Ignoring unrecognised label {key!r} — "
            f"expected one of {', '.join(all_labels())}"
        )
        return None
    match = _FLAG_RE.match(key)
    return min(len(match.group(1)), max(_FLAGS)) if match else None


# ── The review-effort label ──────────────────────────────────────────────────
# Same scoped-label form as the verbosity levels: "review effort::4/5". The scope
# is the point — GitLab keeps one label per scope, so a re-estimate replaces the
# previous value instead of leaving an MR carrying both "3/5" and "4/5".

#: Default scope for the effort label, matching the labels that exist in the
#: reviewed projects (`review effort::1/5` … `5/5`). **Case matters on the way
#: out**: GitLab creates any label it is handed, so writing `Review effort::4/5`
#: against a project whose label is `review effort::4/5` silently adds a second,
#: near-identical entry to the project's label list rather than reusing it.
#: Overridable per deployment for a project that capitalises differently, the same
#: way `MR_REVIEW_LABEL` already is.
DEFAULT_EFFORT_LABEL_SCOPE = "review effort"


def effort_label_scope() -> str:
    """The effort label's scope, from MR_EFFORT_LABEL_SCOPE. Read at call time."""
    return os.getenv("MR_EFFORT_LABEL_SCOPE", "").strip() or DEFAULT_EFFORT_LABEL_SCOPE


def effort_label(effort: int) -> str:
    """The label to apply for an effort estimate, e.g. ``review effort::4/5``."""
    return f"{effort_label_scope()}::{effort}/5"


def has_effort_label(labels: Optional[Iterable[Any]]) -> bool:
    """True when an effort label is already on the MR.

    Matching is **case-insensitive** and accepts both the scoped
    ``review effort::4/5`` and the original space-separated ``Review effort 4/5``.
    Both have to match, or an MR labelled before the scope was adopted gets a
    *second*, differently-formatted effort label on its next run — this is the same
    check that suppresses re-estimation, so a miss means a duplicate.
    """
    pattern = re.compile(
        rf"^{re.escape(effort_label_scope())}\s*(?:::|\s)\s*\d+\s*/\s*5$", re.I
    )
    for raw in labels or ():
        name = raw.get("name", "") if isinstance(raw, dict) else raw
        if pattern.match(str(name or "").strip()):
            return True
    return False


def cap_comments(issues: list, verbosity: Verbosity) -> Tuple[list, list]:
    """Enforce the level's comment cap. Returns ``(kept, over_cap)``.

    Deterministic backstop for the prompt's budget, the same belt-and-braces the
    suggestion field and the summary length get: the prompt states the cap, and
    this is the last writer before posting. A model that returns 40 comments at
    ``-vvv`` must not post 40.

    The cut is input order, because at this point the list is either already ranked
    by the model (``rank=True``) or deliberately unranked (``rank=False``) — there
    is nothing left to sort by. The caller reports what was cut.
    """
    limit = max(0, verbosity.max_comments)
    return issues[:limit], issues[limit:]
