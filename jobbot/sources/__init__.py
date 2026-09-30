import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from jobbot.models import Company, Job
from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import BoardNotFound, Source
from jobbot.sources.greenhouse import GreenhouseSource

log = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 0.5  # pause between boards, to be polite

SOURCES: dict[str, Source] = {s.name: s for s in (GreenhouseSource(), AshbySource())}


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # company -> job count
    failures: dict[str, str] = field(default_factory=dict)  # company -> reason


def fetch_all(
    companies: list[Company],
    client: httpx.Client,
    delay: float = REQUEST_DELAY_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> FetchResult:
    """Fetch every company's board sequentially. One bad board never stops the run."""
    sleep = sleep or time.sleep
    result = FetchResult()
    for i, company in enumerate(companies):
        if i and delay:
            sleep(delay)
        source = SOURCES.get(company.ats)
        if source is None:
            reason = f"no adapter for ats {company.ats!r} yet"
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
