from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AtsType = Literal["greenhouse", "ashby", "lever"]


def utcnow() -> datetime:
    return datetime.now(UTC)


class Company(BaseModel):
    """One entry in companies.yaml. Unknown fields are rejected so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")

    name: str
    ats: AtsType
    slug: str
    priority: Literal[1, 2] = 2  # 1 = hit list (emailed immediately), 2 = everyone else
    enabled: bool = True


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
    posted_at: datetime | None = None  # true first-published date only; never last-updated
    first_seen_at: datetime = Field(default_factory=utcnow)
    compensation: str | None = None  # human-readable range, when the ATS publishes one

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.source_job_id)


class Match(BaseModel):
    """A job that passed every filter, plus what we learned while filtering it."""

    job: Job
    score: int
    min_yoe: float | None  # lowest stated YOE requirement; None if none found
    keywords: list[str]
