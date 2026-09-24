from datetime import datetime
from typing import Any

from jobbot.models import Job
from jobbot.sources.base import Source, html_to_text, mentions_remote


class AshbySource(Source):
    name = "ashby"

    def board_url(self, slug: str) -> str:
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"

    def parse(self, payload: Any, company: str) -> list[Job]:
        jobs = []
        for raw in payload.get("jobs", []):
            if raw.get("isListed") is False:
                continue
            # Keep every location in one string so the location filter sees
            # secondary options like "Remote (US)".
            locations = [raw.get("location") or ""] + [
                s.get("location") or "" for s in raw.get("secondaryLocations") or []
            ]
            locations = [loc.strip() for loc in locations if loc and loc.strip()]
            # Ashby's `isRemote` is true for Hybrid roles too, so don't trust it alone.
            is_remote = raw.get("workplaceType") == "Remote" or mentions_remote(*locations)
            description = raw.get("descriptionPlain") or html_to_text(
                raw.get("descriptionHtml") or ""
            )
            published = raw.get("publishedAt")
            jobs.append(
                Job(
                    source=self.name,
                    source_job_id=raw["id"],
                    company=company,
                    title=raw["title"].strip(),
                    location="; ".join(locations),
                    is_remote=is_remote,
                    url=raw["jobUrl"],
                    description_text=description.strip(),
                    posted_at=datetime.fromisoformat(published) if published else None,
                )
            )
        return jobs
