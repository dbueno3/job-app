"""What to email, and when.

- Priority-1 (hit list) matches: emailed immediately, on the run that finds them.
- Everyone else: the regular digest, sent on the first run at or after 7am and 9pm
  America/New_York. Cron is UTC, so the window check happens here with zoneinfo,
  which handles DST.
- `jobbot digest` (force): everything pending right now, one email, hit list on top.

Two meta timestamps are kept separately:
  digest_window_done   the latest window already handled, even if nothing was sent;
                       without it, a match found at 10am would go out at 10am.
  last_digest_sent_at  when a regular digest last actually went out; sets the
                       LinkedIn footer's "posted within" window.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jobbot.config import LinkedInSearch
from jobbot.digest import Digest, build_alert, build_digest
from jobbot.links import job_search_url
from jobbot.models import utcnow
from jobbot.pipeline import DetailFetcher, Evaluator, IngestResult, RefilterResult, ingest, refilter
from jobbot.sources import Listing
from jobbot.storage import Storage

ET = ZoneInfo("America/New_York")
DIGEST_TIMES = (time(7, 0), time(21, 0))
DEFAULT_FOOTER_WINDOW = timedelta(hours=12)
WINDOW_DONE = "digest_window_done"
LAST_SENT = "last_digest_sent_at"


def latest_window(now: datetime) -> datetime:
    """Most recent 7am/9pm ET at or before `now`, as an aware datetime."""
    local = now.astimezone(ET)
    for days_back in (0, 1):
        day = (local - timedelta(days=days_back)).date()
        for t in sorted(DIGEST_TIMES, reverse=True):
            start = datetime.combine(day, t, tzinfo=ET)
            if start <= local:
                return start
    raise AssertionError("unreachable: yesterday always has a window")


def next_window(now: datetime) -> datetime:
    local = now.astimezone(ET)
    for days_ahead in (0, 1):
        day = (local + timedelta(days=days_ahead)).date()
        for t in sorted(DIGEST_TIMES):
            start = datetime.combine(day, t, tzinfo=ET)
            if start > local:
                return start
    raise AssertionError("unreachable: tomorrow always has a window")


def digest_due(now: datetime, window_done: datetime | None) -> bool:
    return window_done is None or window_done < latest_window(now)


def footer_links(
    searches: list[LinkedInSearch], now: datetime, last_sent: datetime | None
) -> list[tuple[str, str]]:
    since = (now - last_sent) if last_sent else DEFAULT_FOOTER_WINDOW
    seconds = int(since.total_seconds())
    return [(s.label, job_search_url(s.location, seconds, s.remote)) for s in searches]


@dataclass
class Plan:
    """What the first attempt sent; re-applied (never re-sent) on retries."""

    emailed: list[tuple[str, str]] = field(default_factory=list)
    window_done: datetime | None = None
    digest_sent_at: datetime | None = None
    alerts: int = 0
    digest: int = 0
    next_digest: datetime | None = None


@dataclass
class RunState:
    """Carried across publish attempts so a retry re-applies but never re-sends."""

    plan: Plan | None = None
    ingest: IngestResult | None = None
    refilter: RefilterResult | None = None
    pruned: int = 0

    @property
    def digested(self) -> list[tuple[str, str]] | None:
        return self.plan.emailed if self.plan else None


def plan_and_send(
    store: Storage,
    send: Callable[[Digest], None],
    priorities: dict[str, int],
    searches: list[LinkedInSearch],
    now: datetime,
    force: bool = False,
) -> Plan:
    """Decide what goes out this run and send it."""
    plan = Plan()
    pending = store.pending_digest()
    is_hit = [priorities.get(s.job.company, 2) == 1 for s in pending]
    hits = [s for s, hit in zip(pending, is_hit, strict=True) if hit]
    rest = [s for s, hit in zip(pending, is_hit, strict=True) if not hit]
    window_done = store.get_meta(WINDOW_DONE)
    last_sent = store.get_meta(LAST_SENT)
    last_sent_at = datetime.fromisoformat(last_sent) if last_sent else None
    due = force or digest_due(now, datetime.fromisoformat(window_done) if window_done else None)

    if force:
        sections = [("Hit list", hits), ("New matches", rest)]
        to_digest = hits + rest
    else:
        if hits:
            send(build_alert(hits, now))
            plan.emailed += [s.job.key for s in hits]
            plan.alerts = len(hits)
        sections = [("New matches", rest)]
        to_digest = rest if due else []

    if to_digest:
        send(build_digest(sections, now, footer_links(searches, now, last_sent_at)))
        plan.emailed += [s.job.key for s in to_digest]
        plan.digest = len(to_digest)
        plan.digest_sent_at = now
    if due:
        plan.window_done = latest_window(now)
    else:
        plan.next_digest = next_window(now)
    return plan


def apply_plan(store: Storage, plan: Plan, now: datetime) -> None:
    store.mark_digested(plan.emailed, now)
    if plan.window_done:  # stored as UTC, like every other timestamp in the DB
        store.set_meta(WINDOW_DONE, plan.window_done.astimezone(UTC).isoformat())
    if plan.digest_sent_at:
        store.set_meta(LAST_SENT, plan.digest_sent_at.astimezone(UTC).isoformat())


def run_once(
    db_path: Path,
    listings: list[Listing],
    evaluator: Evaluator,
    details: DetailFetcher,
    state: RunState,
    send: Callable[[Digest], None],
    priorities: dict[str, int] | None = None,
    searches: list[LinkedInSearch] | None = None,
    do_refilter: bool = False,
    dry_run: bool = False,
    force_digest: bool = False,
    now: datetime | None = None,
) -> None:
    """Ingest -> (refilter) -> alerts + digest -> mark emailed -> prune, on one DB file.

    Safe to call again on a newer copy of the DB (after a rejected push): ingest is
    idempotent, details are cached, and emails go out only on the first call.
    """
    now = now or utcnow()
    with Storage(db_path) as store:
        state.ingest = ingest(store, listings, evaluator, details, now)
        if do_refilter:
            state.refilter = refilter(store, listings, evaluator, details, now)
        if not dry_run:
            store.commit()  # ingestion is kept even if an email fails below
        if state.plan is None:
            state.plan = plan_and_send(
                store, send, priorities or {}, searches or [], now, force=force_digest
            )
        apply_plan(store, state.plan, now)
        state.pruned = store.prune_descriptions(now)
        if dry_run:
            store.rollback()
        else:
            store.commit()
