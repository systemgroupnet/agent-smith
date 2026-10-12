"""Which reviewer-file pairs are already reviewed, carried from one run to the next.

The incremental review used to remember exactly one thing: the last commit a run
finished cleanly. That is all-or-nothing, and production made the cost of the
"nothing" visible. 61% of runs end on ``OVERALL_TIMEOUT_S``, and a timeout — or any
single failed file — holds the marker. An MR whose first run never finished
cleanly has no marker at all, so every later run is a full review again. Because
the queue order is deterministic, each of those runs re-reviews the same leading
files and stops at the same tail; 136 MRs got five or more consecutive full reviews
(10.5 on average). Every re-run re-found and re-posted the same findings, and
duplicate comments are the developers' first complaint. Some files can never pass
the time budget at all (one Go file failed 27 runs out of 27, every time on "Review
time budget reached"), so for those MRs the marker never advanced, ever.

So the marker note now also carries a **progress record**: per ``(path,
reviewer)`` pair, the git blob it was reviewed at, plus a failure count for the
pairs that ended without recording anything or raised. A re-run skips every pair
already reviewed at the file's current content, dispatches what is left —
never-attempted pairs first — and gives up on a pair that has failed
``REVIEW_MAX_FILE_FAILURES`` times at the same content, so one unreviewable file can
no longer pin the marker.

Three decisions shape it:

* **The key is the blob, not the commit.** A commit says nothing about whether one
  file changed; a blob id is the file's content by definition. So a push that
  touches one file invalidates that file's pairs and nothing else, and a rebase
  (where the compare API fails and the run falls back to a full review) still
  skips every file whose content survived it.
* **Per pair, not per file.** A Go service file is reviewed by two reviewers, and
  a run that truncates between them has reviewed half of it. Recording the file as
  done would lose the other half for good; recording nothing would re-post the
  first half.
* **Verbosity is part of "done".** Reviewers record every severity, but a run only
  posts what its level lets through — a pair reviewed at the default level had its
  mediums discarded, and one reviewed at ``-vv`` had its highs *ranked*, so a high
  that lost its slot there was never posted although ``-v`` posts every high. A
  record is trusted only when its level **covers** the current one (:func:`covers`):
  everything the current level would post, the recorded one posted too. Level
  numbers are not that order — ``-vv`` is louder than ``-v`` and does not cover it.

The record is serialised into a hidden HTML comment —
``<!-- code-review-agent:progress:v1 <base64url(zlib(json))> -->`` — base64 so a
path containing ``-->`` cannot close the comment early, zlib because a large MR's
record is mostly repeated reviewer names and path prefixes. Everything here is
pure: no I/O, no clock, no environment beyond reading one setting.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import zlib
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from core.utils.review_verbosity import DEFAULT, Verbosity, all_labels, parse_verbosity

#: ``(repository path, reviewer name)`` — the unit the dispatch plan is made of.
Pair = Tuple[str, str]

#: Hidden-comment prefix; the version rides in the token so a reader can refuse a
#: format it does not know before touching the payload.
PROGRESS_TOKEN = "code-review-agent:progress"
PROGRESS_VERSION = 1

#: The payload is captured loosely — everything up to the closing ``-->`` — so a
#: corrupted one is *reported* as unreadable rather than silently not matching and
#: passing for "no record". base64 never contains ``-->``, so the first one ends it.
_PROGRESS_RE = re.compile(
    r"<!--\s*" + re.escape(PROGRESS_TOKEN) + r":v(\d+)\s+(.*?)\s*-->", re.S
)

#: Ceiling on the decompressed payload. The note is read only from the agent's own
#: user where that is knowable, but a few lines buy immunity from a zlib bomb
#: whoever wrote it. A 2,000-file MR's record is a few hundred KiB.
MAX_DECODED_BYTES = 8 * 1024 * 1024

#: Failures at the same content after which a pair is given up on. Two, because one
#: failure is weather — a slow provider, a salvage pass that timed out — and a
#: second at the *same* blob is the file: the 27-of-27 case. ``0`` disables
#: abandonment entirely (every failure is retried forever, the old behaviour).
MAX_FILE_FAILURES_ENV = "REVIEW_MAX_FILE_FAILURES"
DEFAULT_MAX_FILE_FAILURES = 2

#: A run that failed on at least this many files *and* failed more pairs than it
#: completed is treated as an outage rather than as evidence against each file:
#: nobody's failure count moves (:func:`is_outage`). Without it, two runs against a
#: provider too slow to finish most reviews would abandon those pairs and advance
#: the marker over files nobody read — the one outcome the marker exists to prevent.
#:
#: The guard used to need *zero* completions, which a slow-but-alive provider
#: defeats: the tiny files finish, every real one times out, and one completion was
#: enough to charge them all — a 10-file MR with 2 completions and 8 timeouts lost
#: those 8 for good after two such runs. Comparing failures with completions asks
#: what one run can actually tell: when the provider finished fewer pairs than it
#: lost, the failures cannot be told apart from weather, and guessing wrong costs
#: unread code where not guessing costs one more run. A tie is charged.
#:
#: The floor is in *files* (a service file's two reviewers failing is one file's
#: evidence), the comparison in pairs (the unit completion is counted in). Below the
#: floor a failure is always charged, which is what lets a lone unreviewable file at
#: the tail of an MR — the case this module exists for — eventually be abandoned,
#: whether or not anything else was left to complete beside it. The mirror cost:
#: three or more files that genuinely cannot be reviewed, with fewer completions
#: beside them, are never charged, since one run's evidence cannot tell them from an
#: outage; the marker holds rather than guessing.
OUTAGE_MIN_FILES = 3


def max_file_failures() -> int:
    """``REVIEW_MAX_FILE_FAILURES``, default 2; ``0`` means never abandon."""
    raw = (os.getenv(MAX_FILE_FAILURES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAX_FILE_FAILURES
    try:
        return max(0, int(raw))
    except ValueError:
        return DEFAULT_MAX_FILE_FAILURES


@dataclass
class ProgressRecord:
    """What previous runs settled, keyed by pair.

    Serialised as ``{v, head, verbosity, done: [[path, reviewer, blob]], fail:
    [[path, reviewer, blob, n]]}``. ``head`` is informational — the commit the record
    was written at — because validity is decided per blob, never per commit.
    """

    head: Optional[str] = None
    #: The verbosity *level* (``Verbosity.level``) the ``done`` entries were posted at.
    verbosity: int = 0
    #: pair -> blob it was reviewed (and its findings posted) at.
    done: Dict[Pair, str] = field(default_factory=dict)
    #: pair -> (blob, consecutive failures at that blob).
    fail: Dict[Pair, Tuple[str, int]] = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.done and not self.fail

    def to_dict(self) -> dict:
        return {
            "v": PROGRESS_VERSION,
            "head": self.head,
            "verbosity": int(self.verbosity),
            # Sorted so the same record always serialises to the same bytes.
            "done": [[p, r, b] for (p, r), b in sorted(self.done.items())],
            "fail": [[p, r, b, n] for (p, r), (b, n) in sorted(self.fail.items())],
        }

    @classmethod
    def from_dict(cls, data: object) -> "ProgressRecord":
        """Rebuild a record; raises ``ValueError`` on a wrong version or shape.

        A malformed *entry* is skipped rather than failing the record: losing one
        pair costs one re-review, losing the record costs all of them.
        """
        if not isinstance(data, dict):
            raise ValueError("progress record is not an object")
        if data.get("v") != PROGRESS_VERSION:
            raise ValueError(f"progress record version {data.get('v')!r} is not {PROGRESS_VERSION}")
        head = data.get("head")
        try:
            verbosity = int(data.get("verbosity") or 0)
        except (TypeError, ValueError):
            raise ValueError("progress record has no usable verbosity")
        record = cls(head=head if isinstance(head, str) else None, verbosity=verbosity)
        for entry in data.get("done") or []:
            if (isinstance(entry, list) and len(entry) == 3
                    and all(isinstance(x, str) and x for x in entry)):
                record.done[(entry[0], entry[1])] = entry[2]
        for entry in data.get("fail") or []:
            if (isinstance(entry, list) and len(entry) == 4
                    and all(isinstance(x, str) and x for x in entry[:3])
                    and isinstance(entry[3], int) and entry[3] > 0):
                record.fail[(entry[0], entry[1])] = (entry[2], entry[3])
        return record


def encode(record: ProgressRecord) -> str:
    """The hidden HTML comment carrying ``record``."""
    raw = json.dumps(record.to_dict(), separators=(",", ":"), sort_keys=True).encode()
    payload = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode("ascii")
    return f"<!-- {PROGRESS_TOKEN}:v{PROGRESS_VERSION} {payload} -->"


def decode(text: Optional[str]) -> Tuple[ProgressRecord, Optional[str]]:
    """Read the progress comment out of a note body.

    Returns ``(record, error)``. No comment at all is an empty record and no error —
    a note written before this feature, or a first run. A comment that is present but
    unreadable (garbage, truncated, another version) is also an empty record, with
    an error the caller records as a ``review_progress`` warning: the cost is one
    run that reviews more than it needed to, which is the pre-feature behaviour, and
    never a run that skips something it should not.
    """
    match = _PROGRESS_RE.search(text or "")
    if not match:
        return ProgressRecord(), None
    version = int(match.group(1))
    if version != PROGRESS_VERSION:
        return ProgressRecord(), (
            f"review progress record is version {version}; this reviewer reads "
            f"v{PROGRESS_VERSION}, so every pair is treated as not yet reviewed"
        )
    try:
        payload = match.group(2)
        if not re.fullmatch(r"[A-Za-z0-9_\-]+=*", payload):
            raise ValueError("payload is not base64url")
        compressed = base64.urlsafe_b64decode(payload.encode("ascii"))
        inflater = zlib.decompressobj()
        raw = inflater.decompress(compressed, MAX_DECODED_BYTES)
        if inflater.unconsumed_tail:
            raise ValueError(f"payload inflates past {MAX_DECODED_BYTES} bytes")
        record = ProgressRecord.from_dict(json.loads(raw.decode("utf-8")))
    except (binascii.Error, zlib.error, UnicodeDecodeError, ValueError, TypeError) as e:
        return ProgressRecord(), (
            f"could not read the review progress record ({type(e).__name__}: {e}); "
            f"every pair is treated as not yet reviewed"
        )
    return record, None


#: Every verbosity level ``review_verbosity`` defines, by number — read through its
#: public resolver rather than its private table, so a level added there is known
#: here without a second list to keep in step.
_LEVELS: Dict[int, Verbosity] = {
    DEFAULT.level: DEFAULT,
    **{v.level: v for v in (parse_verbosity([label]) for label in all_labels())},
}


def covers(recorded: int, current: int) -> bool:
    """Whether a pair done at level ``recorded`` is done at level ``current`` too.

    True when everything the current level would post, the recorded one posted as
    well — derived from the level definitions, not their numbers, because the
    levels are not nested: ``-vv`` (highs and mediums, *ranked* down to 30) is louder
    than ``-v`` (*every* high, up to 30) and does not cover it, since a high that
    ``-vv`` ranked out was never posted. Three conditions, all needed:

    * the recorded level's severities include the current one's;
    * its cap is at least as large — a keep-all level still cuts at its cap;
    * it kept everything that qualified, or the current level ranks too (a ranked
      selection covers only another ranked one, and only with at least its budget).

    Equal levels always cover. A level this reviewer does not define (a record from
    a newer version, a hand-edited note) covers nothing else. The relation is
    transitive, which is what makes :func:`merge` re-labelling a covering record
    with the current level sound.
    """
    if recorded == current:
        return True
    r, c = _LEVELS.get(recorded), _LEVELS.get(current)
    if r is None or c is None:
        return False
    return (
        set(r.severities) >= set(c.severities)
        and r.max_comments >= c.max_comments
        and (not r.rank or c.rank)
    )


def _blob(blobs: Optional[Mapping[str, str]], path: str) -> Optional[str]:
    return blobs.get(path) if blobs else None


def pending_pairs(
    plan_pairs: Sequence[Pair],
    record: ProgressRecord,
    blobs: Optional[Mapping[str, str]],
    verbosity: int,
) -> List[Pair]:
    """The pairs of ``plan_pairs`` still to review, in plan order.

    A pair is skipped only when the record says it was reviewed at the blob the file
    has *now*, at a verbosity that :func:`covers` the current run's. An unknown blob
    (the file is missing from the checkout, or ``blobs`` could not be read) never
    skips: a record can only ever save work, not lose it.
    """
    trusted = covers(record.verbosity, verbosity)
    pending: List[Pair] = []
    for pair in plan_pairs:
        blob = _blob(blobs, pair[0])
        if trusted and blob is not None and record.done.get(pair) == blob:
            continue
        pending.append(pair)
    return pending


def abandoned(
    record: ProgressRecord, blobs: Optional[Mapping[str, str]], max_failures: int
) -> Set[Pair]:
    """Pairs that have failed ``max_failures`` times at the file's current content."""
    if max_failures <= 0 or not blobs:
        return set()
    return {
        pair for pair, (blob, n) in record.fail.items()
        if n >= max_failures and _blob(blobs, pair[0]) == blob
    }


def failure_counts(
    record: ProgressRecord, blobs: Optional[Mapping[str, str]]
) -> Dict[Pair, int]:
    """Prior failures per pair at the current content — what the queue orders by."""
    return {
        pair: n for pair, (blob, n) in record.fail.items()
        if _blob(blobs, pair[0]) == blob
    }


def merge(
    old: ProgressRecord,
    completed: Iterable[Pair],
    failed: Iterable[Pair],
    blobs: Mapping[str, str],
    current_paths: Iterable[str],
    *,
    verbosity: int,
    head: Optional[str] = None,
) -> ProgressRecord:
    """The record to write after this run.

    * Old entries survive only while their file is still in the MR at the same blob
      — a changed file starts over, and a file removed from the MR drops out, which
      is what keeps the record bounded by the MR rather than by its history.
    * Old ``done`` entries written at a level that does not :func:`cover <covers>`
      the current one are dropped: they were not trusted this run, and keeping them
      under the new level would claim a coverage they never had. Entries from a
      covering level are kept and re-labelled with the current one, which is the
      most the whole record can now promise.
    * A completed pair is done at its current blob and its failure count is
      cleared; a failed pair's count goes up by one at the same blob and restarts
      at one on a new blob.
    """
    current = set(current_paths)

    def live(path: str, blob: str) -> bool:
        return path in current and blobs.get(path) == blob

    new = ProgressRecord(head=head, verbosity=verbosity)
    if covers(old.verbosity, verbosity):
        for pair, blob in old.done.items():
            if live(pair[0], blob):
                new.done[pair] = blob
    for pair, (blob, n) in old.fail.items():
        if live(pair[0], blob):
            new.fail[pair] = (blob, n)

    completed = set(completed)
    for pair in completed:
        blob = blobs.get(pair[0])
        if blob is None or pair[0] not in current:
            continue
        new.done[pair] = blob
        new.fail.pop(pair, None)
    for pair in set(failed) - completed:
        blob = blobs.get(pair[0])
        if blob is None or pair[0] not in current:
            continue
        prior = new.fail.get(pair)
        count = prior[1] + 1 if prior and prior[0] == blob else 1
        new.fail[pair] = (blob, count)
        new.done.pop(pair, None)
    return new


def is_outage(completed: Iterable[Pair], failed: Iterable[Pair]) -> bool:
    """Whether one run's failures look like the provider's rather than the files'.

    At least ``OUTAGE_MIN_FILES`` files failed and more pairs failed than completed —
    see ``OUTAGE_MIN_FILES`` for why both, and why a tie is not an outage.
    """
    completed = set(completed)
    failed = set(failed) - completed
    return (len({path for path, _ in failed}) >= OUTAGE_MIN_FILES
            and len(failed) > len(completed))


def charged_failures(completed: Iterable[Pair], failed: Iterable[Pair]) -> Set[Pair]:
    """The failures that count against their file — all of them, unless the run
    looks like an outage (:func:`is_outage`)."""
    completed, failed = set(completed), set(failed)
    return set() if is_outage(completed, failed) else failed


@dataclass
class DispatchDecision:
    """What one run should do with its plan, given the record it inherited."""

    #: Reviewed at this content and verbosity by an earlier run.
    already_done: Set[Pair] = field(default_factory=set)
    #: Failed ``max_failures`` times at this content; not retried until it changes.
    abandoned: Set[Pair] = field(default_factory=set)
    #: Prior failures per pair, for ordering the queue.
    failure_counts: Dict[Pair, int] = field(default_factory=dict)

    @property
    def skip(self) -> Set[Pair]:
        return self.already_done | self.abandoned


def decide(
    plan_pairs: Sequence[Pair],
    record: ProgressRecord,
    blobs: Optional[Mapping[str, str]],
    verbosity: int,
    max_failures: int,
) -> DispatchDecision:
    """Split the plan into already-done, abandoned and to-review."""
    pending = pending_pairs(plan_pairs, record, blobs, verbosity)
    pending_set = set(pending)
    return DispatchDecision(
        already_done={pair for pair in plan_pairs if pair not in pending_set},
        abandoned=abandoned(record, blobs, max_failures) & pending_set,
        failure_counts=failure_counts(record, blobs),
    )


@dataclass
class Settlement:
    """The outcome of one run, ready to be written back."""

    record: ProgressRecord
    #: Plan pairs neither reviewed at their current content nor given up on.
    pending: List[Pair]
    #: Pairs that reached the failure ceiling in *this* run.
    newly_abandoned: Set[Pair]
    #: The run was judged an outage and charged nobody.
    outage: bool

    @property
    def settled(self) -> bool:
        """Every pair of the plan is done or abandoned — advance the commit marker."""
        return not self.pending


def settle(
    old: ProgressRecord,
    plan_pairs: Sequence[Pair],
    completed: Iterable[Pair],
    failed: Iterable[Pair],
    blobs: Mapping[str, str],
    current_paths: Iterable[str],
    *,
    verbosity: int,
    head: Optional[str],
    max_failures: int,
) -> Settlement:
    """Merge one run's outcome into the record and decide whether the MR is covered."""
    completed = set(completed)
    failed = set(failed)
    charged = charged_failures(completed, failed)
    before = abandoned(old, blobs, max_failures)
    record = merge(old, completed, charged, blobs, current_paths,
                   verbosity=verbosity, head=head)
    after = abandoned(record, blobs, max_failures)
    return Settlement(
        record=record,
        pending=[pair for pair in pending_pairs(plan_pairs, record, blobs, verbosity)
                 if pair not in after],
        newly_abandoned=after - before,
        outage=bool(failed) and not charged,
    )
