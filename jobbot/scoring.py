import re

from jobbot.config import ScoringConfig
from jobbot.models import Job, Match


class Scorer:
    """Counts distinct keyword hits; a keyword in the title is worth more than in the body.

    Distinct (not total) hits so a long description that says "Python" ten times
    doesn't outrank a short one that says it once.
    """

    def __init__(self, cfg: ScoringConfig) -> None:
        self.cfg = cfg
        self.patterns = {name: re.compile(p, re.IGNORECASE) for name, p in cfg.keywords.items()}

    def score(self, job: Job) -> tuple[int, list[str]]:
        total = 0
        matched = []
        for name, pattern in self.patterns.items():
            in_title = bool(pattern.search(job.title))
            in_desc = bool(pattern.search(job.description_text))
            total += self.cfg.title_weight * in_title + self.cfg.description_weight * in_desc
            if in_title or in_desc:
                matched.append(name)
        return total, matched


def sort_matches(matches: list[Match]) -> list[Match]:
    """Highest score first, then most recently posted; undated jobs last within a score."""

    def key(m: Match) -> tuple[int, float]:
        posted = m.job.posted_at.timestamp() if m.job.posted_at else float("-inf")
        return (-m.score, -posted)

    return sorted(matches, key=key)
