import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from jobbot.config import FilterConfig, Profile
from jobbot.filters import JobFilter
from jobbot.models import Company, Job, Match, utcnow
from jobbot.scoring import Scorer
from jobbot.sources import REQUEST_DELAY_SECONDS, SOURCES, Listing
from jobbot.storage import Storage

log = logging.getLogger(__name__)


class DetailFetcher:
    """Fetches per-job descriptions for sources whose listings lack them.

    Results are cached by job key, so replaying the same run against a newer DB
    (after a push is rejected) never refetches.
    """

    def __init__(
        self,
        client: httpx.Client | None,
        delay: float = REQUEST_DELAY_SECONDS,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.client = client
        self.delay = delay
        self.sleep = sleep or time.sleep
        self.cache: dict[tuple[str, str], Job | None] = {}
        self.requests = 0
        self.failures = 0

    def fill(self, company: Company, job: Job) -> Job:
        """Job with its description filled in; description stays None if it can't be fetched."""
        source = SOURCES.get(job.source)
        if job.description_text is not None or source is None or not source.needs_details:
            return job
        if job.key not in self.cache:
            if self.client is None:
                return job
            if self.requests and self.delay:
                self.sleep(self.delay)
            self.requests += 1
            try:
                self.cache[job.key] = source.fetch_details(self.client, company.slug, job)
            except Exception as exc:  # noqa: BLE001 - one bad posting never stops the run
                log.warning("no description for %s %s: %s", company.name, job.title, exc)
                self.failures += 1
                self.cache[job.key] = None
        filled = self.cache[job.key]
        return filled if filled is not None else job


class Evaluator:
    """Filter + score one job into (Match or None, drop reason)."""

    def __init__(self, cfg: FilterConfig, profile: Profile | None = None) -> None:
        self.filter = JobFilter(cfg, profile)
        self.scorer = Scorer(cfg.scoring)

    def evaluate(self, job: Job, now: datetime) -> tuple[Match | None, str | None]:
        ev = self.filter.evaluate(job, now)
        if ev.reason:
            return None, ev.reason
        score, keywords = self.scorer.score(job)
        match = Match(job=job, score=score, min_yoe=ev.min_yoe, keywords=keywords, gaps=ev.gaps)
        return match, None


@dataclass
class IngestResult:
    new: int = 0
    matched: int = 0
    reposts: int = 0
    dropped: Counter = field(default_factory=Counter)  # reason -> count (new jobs only)


def ingest(
    store: Storage,
    listings: list[Listing],
    evaluator: Evaluator,
    details: DetailFetcher,
    now: datetime | None = None,
) -> IngestResult:
    """Store every newly seen job; descriptions only for jobs passing title + location.

    Idempotent: jobs already in the DB are skipped, so re-running the same listings
    against a newer copy of the DB (push retry) changes nothing twice. Does not commit.
    """
    now = now or utcnow()
    result = IngestResult()
    for listing in listings:
        for job in listing.jobs:
            if store.has(job):
                continue
            if evaluator.filter.passes_title_location(job):
                job = details.fill(listing.company, job)
            else:
                job = job.model_copy(update={"description_text": None})  # metadata only
            match, reason = evaluator.evaluate(job, now)
            job_id = store.insert(job, match, reason, now)
            result.new += 1
            if match:
                result.matched += 1
            else:
                result.dropped[reason] += 1
            if store.get(job_id).is_repost:
                result.reposts += 1
    return result


@dataclass
class RefilterResult:
    checked: int = 0
    newly_matched: int = 0
    newly_unmatched: int = 0
    descriptions_fetched: int = 0


def refilter(
    store: Storage,
    listings: list[Listing],
    evaluator: Evaluator,
    details: DetailFetcher,
    now: datetime | None = None,
) -> RefilterResult:
    """Re-apply current filters to every stored job (after filters.yaml changes).

    Jobs that now pass title + location but have no stored description get one on
    demand: per-job fetch (Greenhouse) or from this run's listing (Ashby). Status,
    notes, and digest state are untouched; newly matched jobs join the next digest.
    Does not commit.
    """
    now = now or utcnow()
    listed = {j.key: (lst.company, j) for lst in listings for j in lst.jobs}
    companies = {lst.company.name: lst.company for lst in listings}
    result = RefilterResult()
    for job_id in store.all_ids():
        stored = store.get(job_id)
        job = stored.job
        result.checked += 1
        if job.description_text is None and evaluator.filter.passes_title_location(job):
            company, fresh = listed.get(job.key, (companies.get(job.company), None))
            if fresh is not None and fresh.description_text is not None:
                job = job.model_copy(update={"description_text": fresh.description_text})
            elif company is not None:
                job = details.fill(company, job)
            if job.description_text is not None:
                store.set_details(job_id, job.description_text, job.compensation)
                result.descriptions_fetched += 1
        match, reason = evaluator.evaluate(job, now)
        if match and not stored.matched:
            result.newly_matched += 1
        elif not match and stored.matched:
            result.newly_unmatched += 1
        store.update_evaluation(job_id, match, reason, now)
    return result
