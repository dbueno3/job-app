import re
from datetime import datetime, timedelta

from jobbot.config import FilterConfig, LocationConfig
from jobbot.models import Job

# --- term matching ------------------------------------------------------------


def compile_terms(terms: list[str]) -> re.Pattern:
    """Case-insensitive match of any term (or its plural), not touching other word characters.

    Lookarounds instead of \\b so terms ending in punctuation ("u.s.") still work.
    """
    alternation = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})s?(?!\w)", re.IGNORECASE)


# --- title --------------------------------------------------------------------


def title_ok(title: str, exclude: re.Pattern, include: re.Pattern, require: re.Pattern) -> bool:
    """No exclude term, at least one include term, and at least one require term.

    Exclusions win: 'Senior Software Engineer' is dropped even though it includes SWE.
    """
    return not exclude.search(title) and bool(include.search(title)) and bool(require.search(title))


# --- location -----------------------------------------------------------------


class LocationMatcher:
    def __init__(self, cfg: LocationConfig) -> None:
        self.include = compile_terms(cfg.include)
        self.us = compile_terms(cfg.us_markers)
        self.non_us = compile_terms(cfg.non_us_markers)

    def ok(self, job: Job) -> bool:
        """NYC-area jobs, or remote jobs open to the US.

        A remote listing that names only other countries ("Toronto, Canada Remote")
        is dropped; plain "Remote" with no country is assumed US-eligible.
        """
        if self.include.search(job.location):
            return True
        if not job.is_remote:
            return False
        if self.us.search(job.location):
            return True
        return not self.non_us.search(job.location)


# --- recency ------------------------------------------------------------------


def is_recent(job: Job, now: datetime, max_age_days: int, keep_undated: bool) -> bool:
    if job.posted_at is None:
        return keep_undated
    return now - job.posted_at <= timedelta(days=max_age_days)


# --- years of experience --------------------------------------------------------

WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}
_NUM = r"(?:\d{1,2}(?:\.\d)?|" + "|".join(WORD_NUMBERS) + r")"
_PAREN = r"(?:\s*\(\s*\d{1,2}\s*\))?"  # "three (3) years"
_DASH = r"\s*(?:-|–|—|to)\s*"
_PLUS = r"\s*(?:\+|plus\b|or\s+more\b|or\s+greater\b)?"
_YEARS = r"\s*(?:years?|yrs?)\b"

YOE_PATTERN = re.compile(
    # Not preceded by a word char, digit separator, or currency ("$10,000 per year").
    rf"(?<![\w$.,])(?:"
    rf"(?P<lo>{_NUM}){_PAREN}{_DASH}(?P<hi>{_NUM}){_PAREN}\s*\+?{_YEARS}"
    rf"|(?P<n>{_NUM}){_PAREN}{_PLUS}{_YEARS}"
    rf")",
    re.IGNORECASE,
)

# Text just before the number that means it's not a requirement.
_NOT_REQUIREMENT_BEFORE = re.compile(
    r"\b(?:past|last|next|over\s+the|within|first|founded|since|ago|every|per|each|up\s+to)\s*$",
    re.IGNORECASE,
)
# The sentence must look like it's talking about the candidate's experience.
_EXPERIENCE_CONTEXT = re.compile(
    r"\b(?:experience[ds]?|professional|professionally|industry|work|working|worked|"
    r"hands-on|build|building|built|develop|developing|development|engineering|"
    r"programming|coding|software|role|background|track\s+record|minimum|at\s+least|"
    r"require[sd]?)\b",
    re.IGNORECASE,
)
_SENTENCE_BREAK = re.compile(r"[.!?;](?=\s)|\n|•|·")
MAX_PLAUSIBLE_YOE = 15


def _to_number(token: str) -> float:
    token = token.lower()
    return float(WORD_NUMBERS[token]) if token in WORD_NUMBERS else float(token)


def _sentence_around(text: str, start: int, end: int) -> tuple[str, str]:
    """Return (text before the match, whole sentence) bounded by sentence breaks."""
    before = text[:start]
    breaks = list(_SENTENCE_BREAK.finditer(before))
    s_start = breaks[-1].end() if breaks else 0
    after_break = _SENTENCE_BREAK.search(text, end)
    s_end = after_break.start() if after_break else len(text)
    return text[s_start:start], text[s_start:s_end]


def parse_min_yoe(text: str) -> float | None:
    """Lowest stated minimum years of experience in a description, or None.

    "3+ years" -> 3, "2-4 years" -> 2, "minimum of three years" -> 3.
    When several requirements appear, the lowest minimum wins.
    """
    found: list[float] = []
    for m in YOE_PATTERN.finditer(text):
        value = _to_number(m.group("lo") or m.group("n"))
        if value > MAX_PLAUSIBLE_YOE:
            continue
        before, sentence = _sentence_around(text, m.start(), m.end())
        if _NOT_REQUIREMENT_BEFORE.search(before):
            continue
        if not _EXPERIENCE_CONTEXT.search(sentence):
            continue
        found.append(value)
    return min(found) if found else None


def yoe_ok(min_yoe: float | None, max_yoe: float, floor: float = 0) -> bool:
    """Unstated is kept; otherwise the lowest stated minimum must be in [floor, max_yoe]."""
    return min_yoe is None or floor <= min_yoe <= max_yoe


# --- dedupe -------------------------------------------------------------------


def fingerprint(job: Job) -> tuple[str, str, str]:
    """Identity that survives a company reposting the same role under a new ID."""

    def norm(s: str) -> str:
        return " ".join(s.lower().split())

    return (norm(job.company), norm(job.title), norm(job.location))


def dedupe(jobs: list[Job]) -> list[Job]:
    """Drop exact repeats and reposts (same company/title/location), keeping the newest."""
    best: dict[tuple[str, str, str], Job] = {}
    for job in jobs:
        fp = fingerprint(job)
        current = best.get(fp)
        if current is None or _newer(job, current):
            best[fp] = job
    return list(best.values())


def _newer(a: Job, b: Job) -> bool:
    if a.posted_at is None:
        return False
    return b.posted_at is None or a.posted_at > b.posted_at


# --- all together -------------------------------------------------------------


class JobFilter:
    """Compiled filters. evaluate() returns the first failing reason, or None if kept."""

    def __init__(self, cfg: FilterConfig) -> None:
        self.cfg = cfg
        self.exclude = compile_terms(cfg.title_exclude)
        self.include = compile_terms(cfg.title_include)
        self.require = compile_terms(cfg.title_require)
        self.location = LocationMatcher(cfg.locations)
        self.new_grad = [re.compile(p, re.IGNORECASE) for p in cfg.new_grad_patterns]

    def is_new_grad(self, job: Job) -> bool:
        return any(p.search(job.description_text) for p in self.new_grad)

    def evaluate(self, job: Job, now: datetime) -> tuple[str | None, float | None]:
        """(drop_reason or None, parsed min YOE). Cheap checks run before the regex-heavy one."""
        if not is_recent(job, now, self.cfg.max_age_days, self.cfg.keep_undated):
            return "too old", None
        if not title_ok(job.title, self.exclude, self.include, self.require):
            return "title", None
        if not self.location.ok(job):
            return "location", None
        if self.is_new_grad(job):
            return "new grad", None
        min_yoe = parse_min_yoe(job.description_text)
        if not yoe_ok(min_yoe, self.cfg.max_yoe, self.cfg.min_yoe):
            return "yoe", min_yoe
        return None, min_yoe
