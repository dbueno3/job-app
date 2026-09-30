import html
from datetime import datetime
from typing import Any

import httpx

from jobbot.models import Job
from jobbot.sources.base import Source, get_json, html_to_text, mentions_remote


class GreenhouseSource(Source):
    """Greenhouse Job Board API.

    posted_at <- `first_published`. Verified 2026-09-29 against Stripe's board: it stays
    fixed (postings first published in 2023 still say 2023) while `updated_at` moves on
    every edit (686 of 710 jobs had updated_at later than first_published). `updated_at`
    is a last-updated date and is never used.

    Two-stage fetch: the board list is requested WITHOUT content=true (~25x smaller on
    the wire; still has title, location, first_published), so jobs come back with
    description_text=None. fetch_details() then fetches one job's description via the
    per-job endpoint, only for jobs that pass title + location.

    compensation <- per-job `pay_input_ranges` (with pay_transparency=true), when the
    company publishes ranges; many don't, and then it stays None.
    """

    name = "greenhouse"
    needs_details = True

    def board_url(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"

    def detail_url(self, slug: str, job_id: str) -> str:
        return (
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}?pay_transparency=true"
        )

    def fetch_details(self, client: httpx.Client, slug: str, job: Job) -> Job:
        description, compensation = self.parse_details(
            get_json(client, self.detail_url(slug, job.source_job_id))
        )
        return job.model_copy(
            update={"description_text": description, "compensation": compensation}
        )

    def parse_details(self, payload: Any) -> tuple[str, str | None]:
        """(description text, compensation) from a per-job payload. Pure: no I/O."""
        return _description(payload.get("content")), _compensation(
            payload.get("pay_input_ranges") or []
        )

    def parse(self, payload: Any, company: str) -> list[Job]:
        jobs = []
        for raw in payload.get("jobs", []):
            location = ((raw.get("location") or {}).get("name") or "").strip()
            # Present only when the list was requested with content=true.
            description = _description(raw["content"]) if "content" in raw else None
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


def _description(content: str | None) -> str:
    # Greenhouse returns `content` as entity-escaped HTML ("&lt;p&gt;..."),
    # so unescape once to get real HTML, then strip it.
    return html_to_text(html.unescape(content or ""))


def _money(cents: int, currency: str) -> str:
    amount = cents / 100
    text = f"{amount / 1000:.1f}".rstrip("0").rstrip(".") + "K" if amount >= 1000 else f"{amount:g}"
    return f"${text}" if currency == "USD" else f"{currency} {text}"


def _compensation(ranges: list[dict]) -> str | None:
    """ "$148.5K – $303.5K", or the overall span for several ranges (e.g. pay zones)."""
    ranges = [r for r in ranges if r.get("min_cents") and r.get("max_cents")]
    if not ranges:
        return None
    currency = ranges[0].get("currency_type") or "USD"
    low = _money(min(r["min_cents"] for r in ranges), currency)
    high = _money(max(r["max_cents"] for r in ranges), currency)
    suffix = f" across {len(ranges)} ranges" if len(ranges) > 1 else ""
    return f"{low} – {high}{suffix}"
