import logging
from dataclasses import dataclass, field

import httpx

from jobbot.config import Company
from jobbot.models import Job
from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import BoardNotFound, Source
from jobbot.sources.greenhouse import GreenhouseSource

log = logging.getLogger(__name__)

SOURCES: dict[str, Source] = {s.name: s for s in (GreenhouseSource(), AshbySource())}


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # company -> job count
    failures: dict[str, str] = field(default_factory=dict)  # company -> reason


def fetch_all(companies: list[Company], client: httpx.Client) -> FetchResult:
    """Fetch every company's board sequentially. One bad board never stops the run."""
    result = FetchResult()
    for company in companies:
        source = SOURCES.get(company.ats)
        if source is None:
            reason = f"unknown ats type {company.ats!r}"
        else:
            try:
                jobs = source.fetch(client, company.slug, company.name)
            except BoardNotFound:
                reason = f"board not found (404) for slug {company.slug!r}"
            except Exception as exc:  # noqa: BLE001 - isolate per-company failures
                reason = f"{type(exc).__name__}: {exc}"
            else:
                result.jobs.extend(jobs)
                result.counts[company.name] = len(jobs)
                continue
        log.warning("skipping %s: %s", company.name, reason)
        result.failures[company.name] = reason
    return result
