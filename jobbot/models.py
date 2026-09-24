from datetime import UTC, datetime

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class Job(BaseModel):
    """A job posting normalized across all sources.

    Uniquely identified by (source, source_job_id).
    """

    source: str
    source_job_id: str
    company: str
    title: str
    location: str
    is_remote: bool
    url: str
    description_text: str
    posted_at: datetime | None = None
    first_seen_at: datetime = Field(default_factory=utcnow)

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_job_id)


class Match(BaseModel):
    """A job that passed every filter, plus what we learned while filtering it."""

    job: Job
    score: int
    min_yoe: float | None  # lowest stated YOE requirement; None if none found
    keywords: list[str]
