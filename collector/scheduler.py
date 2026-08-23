"""In-process scheduler for the weekly engagement digest.

The digest is scheduled **by the service**, not by a crontab on the host. The
collector already holds the data, the credentials and the code; a schedule that
ships alongside them cannot be lost in a host migration, forgotten when the
container moves, or left pointing at a container name that no longer exists. The
cost is that two problems cron solved for free move in here — *has this week been
posted already?* and *is another worker posting it right now?* — and both are
answered by the ``digest_runs`` claim table rather than by this module.

**A ticker, not a timer.** The thread wakes every ``DIGEST_TICK_S`` and asks "which
week is due, and has anyone dealt with it?" It does not sleep until Monday. That
distinction is the whole reliability argument: a timer is lost on every restart, so
a deploy on Sunday evening silently costs you a digest, and a redeploy loop could
skip weeks indefinitely with nothing to show for it. A ticker recomputes from the
clock and the database on every pass, so restarting is free, a service that was
down at the firing moment posts as soon as it comes back, and the schedule has no
state of its own to corrupt.

**Due means: the ISO week before the one containing the firing moment.** Setting a
later day does not change *which* week is reported, only when it is announced — so
``DIGEST_DAY=wed`` still reports Monday-to-Sunday of the previous week, three days
late.

That has a sharp edge worth stating plainly, because ISO weeks close on **Sunday
midnight**:

    DIGEST_DAY=mon, 09:00  → the week that closed the previous night   (1 day old)
    DIGEST_DAY=sun, 12:00  → the week before the one ending tonight    (7 days old)

A Sunday firing time is therefore always a week behind — the week you are standing
in has not finished yet, so the last *completed* one is the week before it. Monday
is the setting that announces a week while it is still fresh. The compose files
currently ship ``sun``/``12:00`` deliberately, as the cadence chosen for the
initial rollout against a scratch channel; move it to ``mon`` when the digest
points at a real one.

**Only ever the most recent due week.** A service that was down for a fortnight
posts one digest, not three. Catching up would dump a wall of history into the
channel at the moment somebody is already dealing with an outage; the missed weeks
are still there and one command away (``python digest.py --week 2026-W33``).

Nothing here runs at import — ``main.py`` calls ``start()`` explicitly after the
migrations, which keeps the module importable by tests that have no database.
"""

import os
import threading
from datetime import datetime, time as dt_time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import digest
import engagement
import storage

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Default cadence. Fifteen minutes is chosen against the only thing that suffers
# from it: the digest lands up to that late. Anything shorter buys nothing (the
# window it is watching for is weekly) and costs a database round trip.
_DEFAULT_TICK_S = 900

_thread: Optional[threading.Thread] = None
_stop = threading.Event()


def _log(message: str) -> None:
    print(f"[digest-scheduler] {message}", flush=True)


def parse_day(raw: Optional[str]) -> int:
    """``mon``/``Monday``/``0`` → 0 … ``sun`` → 6. Unknown values mean Monday.

    Lenient on purpose: a typo in a cadence setting should delay the digest by a
    few days at worst, never stop the service from starting.
    """
    value = (raw or "").strip().lower()
    if not value:
        return 0
    if value.isdigit():
        return int(value) % 7
    for i, name in enumerate(_DAYS):
        if value.startswith(name):
            return i
    return 0


def parse_hour(raw: Optional[str]) -> int:
    """``9`` → 9, ``11:45`` → 11. Out-of-range or unparseable means 9."""
    value = (raw or "").strip()
    if not value:
        return 9
    head = value.split(":", 1)[0]
    try:
        hour = int(head)
    except ValueError:
        return 9
    return hour if 0 <= hour <= 23 else 9


def parse_minute(raw_hour: Optional[str], raw_minute: Optional[str] = None) -> int:
    """The minute past the hour to fire at.

    Read from ``DIGEST_MINUTE`` when set, otherwise from the ``HH:MM`` form of
    ``DIGEST_HOUR`` — so ``DIGEST_HOUR=11:45`` and
    ``DIGEST_HOUR=11 DIGEST_MINUTE=45`` mean the same thing. Anyone writing
    ``11:45`` expects a quarter to twelve, and silently firing at 11:00 is the
    kind of wrong that nobody notices for a month.

    Explicit ``DIGEST_MINUTE`` wins, so a deployment can override the minute
    without rewriting the hour. Out-of-range or unparseable means 0.
    """
    if raw_minute is not None and str(raw_minute).strip():
        try:
            minute = int(str(raw_minute).strip())
        except ValueError:
            return 0
        return minute if 0 <= minute <= 59 else 0

    value = (raw_hour or "").strip()
    if ":" not in value:
        return 0
    try:
        minute = int(value.split(":", 1)[1].strip())
    except ValueError:
        return 0
    return minute if 0 <= minute <= 59 else 0


def resolve_timezone(name: Optional[str]) -> ZoneInfo:
    """The zone the schedule is expressed in; UTC when unset or unknown.

    ``tzdata`` is a declared dependency precisely so this cannot fail on a slim
    base image whose ``/usr/share/zoneinfo`` may be absent.
    """
    value = (name or "").strip()
    if not value:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        _log(f"unknown DIGEST_TIMEZONE {value!r}; falling back to UTC")
        return ZoneInfo("UTC")


def next_due_week(
    now: datetime, day: int = 0, hour: int = 9, minute: int = 0
) -> Optional[str]:
    """The ISO week whose digest is due as of ``now``, or None if none is yet.

    ``now`` must already be in the schedule's timezone — the caller derives *every*
    date from it, including the week boundary. Deriving the week from UTC while
    comparing against a local firing moment shifts the reported week by one either
    side of Sunday/Monday midnight in any non-UTC zone, which is a bug that would
    only ever show up once a week and look like a fluke.

    Pure: no clock, no database, no environment.
    """
    this_monday = now.date() - timedelta(days=now.weekday())
    fire_at = datetime.combine(
        this_monday + timedelta(days=day),
        dt_time(hour=hour, minute=minute),
        tzinfo=now.tzinfo,
    )
    if now < fire_at:
        return None
    # The day before this week's Monday is in the previous ISO week, whatever the
    # configured firing day is. Note the consequence of a *late* firing day: the
    # ISO week containing the firing moment has not closed yet, so a Sunday
    # setting reports the week before the one ending that night — seven days old.
    # Monday is the setting that announces a week the morning after it closes.
    return engagement.iso_week_key(this_monday - timedelta(days=1))


def enabled() -> bool:
    """Whether to run the scheduler at all.

    **The credentials are the opt-in**, matching how ``LANGFUSE_BASE_URL`` works
    elsewhere in this repo: configure a token and a channel and the digest runs;
    there is no third flag to discover. ``DIGEST_ENABLED`` is an explicit
    off-switch only — and an **empty value means enabled**, unlike the four
    reviewer flags that read ``""`` as off, because here the empty state is
    already covered by "no credentials, no scheduler".
    """
    flag = (os.getenv("DIGEST_ENABLED") or "").strip().lower()
    if flag in ("false", "0", "no", "off"):
        return False
    return bool(
        (os.getenv("MATTERMOST_TOKEN") or "").strip()
        and (os.getenv("MATTERMOST_CHANNEL_ID") or "").strip()
        and (os.getenv("MATTERMOST_URL") or "").strip()
    )


def tick() -> Optional[digest.DigestOutcome]:
    """One pass: work out what is due, claim it, post it, record what happened.

    Returns the outcome, or None when nothing was due or another claimant already
    owns the week. Never raises — a scheduler thread that dies takes every future
    digest with it, and silently, so the last line of defence is here.
    """
    tz = resolve_timezone(os.getenv("DIGEST_TIMEZONE"))
    now = datetime.now(tz)
    due = next_due_week(
        now,
        day=parse_day(os.getenv("DIGEST_DAY")),
        hour=parse_hour(os.getenv("DIGEST_HOUR")),
        minute=parse_minute(os.getenv("DIGEST_HOUR"), os.getenv("DIGEST_MINUTE")),
    )
    if due is None:
        return None

    # Claim first. Everything after this point is the responsibility of exactly
    # one caller, and the claim is not released even if the post fails — see
    # storage.claim_digest_week for why at-most-once is the right trade here.
    try:
        if not storage.claim_digest_week(due):
            return None
    except Exception as e:  # noqa: BLE001 — a database blip must not kill the thread
        _log(f"could not claim {due}: {type(e).__name__}: {e}")
        return None

    _log(f"claimed {due}; building digest")
    try:
        outcome = digest.run_digest(due)
    except Exception as e:  # noqa: BLE001 — run_digest returns, but belt and braces
        storage.mark_digest_failed(due, f"{type(e).__name__}: {e}")
        _log(f"{due} failed while building: {type(e).__name__}: {e}")
        return None

    try:
        if outcome.status == "posted":
            storage.mark_digest_posted(
                due, outcome.channel_id or "", outcome.post_id
            )
            _log(f"posted {due} (post id {outcome.post_id or '?'})")
        elif outcome.status == "empty":
            storage.mark_digest_skipped(due, outcome.error or "no activity")
            _log(f"{due} had no activity; staying quiet")
        else:
            storage.mark_digest_failed(due, outcome.error or "unknown error")
            _log(
                f"{due} FAILED: {outcome.error}. The week stays claimed so it "
                f"cannot be double-posted — send it by hand with "
                f"`python digest.py --week {due}` once the cause is fixed."
            )
    except Exception as e:  # noqa: BLE001
        _log(f"could not record the outcome for {due}: {type(e).__name__}: {e}")
    return outcome


def _loop(tick_seconds: int) -> None:
    while not _stop.is_set():
        try:
            tick()
        except Exception as e:  # noqa: BLE001 — see tick(); this is the backstop
            _log(f"tick raised: {type(e).__name__}: {e}")
        # Event.wait rather than sleep so a shutdown is immediate rather than
        # up to a tick late.
        _stop.wait(tick_seconds)


def start() -> bool:
    """Start the scheduler thread if it is configured. Returns whether it started.

    Idempotent, and safe to call from a module import that a WSGI server may
    perform more than once. Daemon thread: the digest is never worth holding a
    shutdown open for, and any week it was mid-way through stays claimed and
    reportable.
    """
    global _thread

    if not enabled():
        if (os.getenv("DIGEST_ENABLED") or "").strip().lower() in (
            "false", "0", "no", "off"
        ):
            _log("disabled by DIGEST_ENABLED")
        else:
            _log(
                "not started — set MATTERMOST_URL, MATTERMOST_TOKEN and "
                "MATTERMOST_CHANNEL_ID to enable the weekly digest"
            )
        return False

    if _thread is not None and _thread.is_alive():
        return True

    try:
        tick_seconds = max(60, int(os.getenv("DIGEST_TICK_S") or _DEFAULT_TICK_S))
    except ValueError:
        tick_seconds = _DEFAULT_TICK_S

    tz = resolve_timezone(os.getenv("DIGEST_TIMEZONE"))
    day = parse_day(os.getenv("DIGEST_DAY"))
    hour = parse_hour(os.getenv("DIGEST_HOUR"))
    minute = parse_minute(os.getenv("DIGEST_HOUR"), os.getenv("DIGEST_MINUTE"))

    _stop.clear()
    _thread = threading.Thread(
        target=_loop, args=(tick_seconds,), name="digest-scheduler", daemon=True
    )
    _thread.start()
    _log(
        f"started — reporting each closed ISO week on {_DAYS[day]} at "
        f"{hour:02d}:{minute:02d} {tz.key}, checking every {tick_seconds}s"
    )
    return True


def stop() -> None:
    """Ask the thread to finish. Used by tests; the daemon flag covers shutdown."""
    _stop.set()
