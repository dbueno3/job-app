import html
from datetime import datetime
from typing import Any

from jobbot.models import Job
from jobbot.sources.base import Source, html_to_text, mentions_remote


class GreenhouseSource(Source):
    """Greenhouse Job Board API.

    posted_at <- `first_published`. Verified 2026-09-29 against Stripe's board: it stays
    fixed (postings first published in 2023 still say 2023) while `updated_at` moves on
    every edit (686 of 710 jobs had updated_at later than first_published). `updated_at`
    is a last-updated date and is never used.

    compensation: the list endpoint has no pay field, so it stays None.
    """

    name = "greenhouse"

    def board_url(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"

    def parse(self, payload: Any, company: str) -> list[Job]:
        jobs = []
        for raw in payload.get("jobs", []):
            location = ((raw.get("location") or {}).get("name") or "").strip()
            # Greenhouse returns `content` as entity-escaped HTML ("&lt;p&gt;..."),
            # so unescape once to get real HTML, then strip it.
            description = html_to_text(html.unescape(raw.get("content") or ""))
            published = raw.get("first_published")
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=str(raw["id"]),
                    company=company,
                    title=raw["title"].strip(),
                    location=location,
                    is_remote=mentions_remote(location),
                    url=raw["absolute_url"],
                    description_text=description,
                    posted_at=datetime.fromisoformat(published) if published else None,
                )
            )
        return jobs
