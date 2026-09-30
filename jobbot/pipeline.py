from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from jobbot.config import FilterConfig, Profile
from jobbot.filters import JobFilter, dedupe
from jobbot.models import Job, Match, utcnow
from jobbot.scoring import Scorer, sort_matches


@dataclass
class SelectResult:
    matches: list[Match]
    duplicates: int = 0
    dropped: Counter = field(default_factory=Counter)  # reason -> count


def select_matches(
    jobs: list[Job],
    cfg: FilterConfig,
    now: datetime | None = None,
    profile: Profile | None = None,
) -> SelectResult:
    """Dedupe, filter, score, and sort fetched jobs."""
    now = now or utcnow()
    unique = dedupe(jobs)
    job_filter = JobFilter(cfg, profile)
    scorer = Scorer(cfg.scoring)
    result = SelectResult(matches=[], duplicates=len(jobs) - len(unique))
    for job in unique:
        ev = job_filter.evaluate(job, now)
        if ev.reason:
            result.dropped[ev.reason] += 1
            continue
        score, keywords = scorer.score(job)
        result.matches.append(
            Match(job=job, score=score, min_yoe=ev.min_yoe, keywords=keywords, gaps=ev.gaps)
        )
    result.matches = sort_matches(result.matches)
    return result
