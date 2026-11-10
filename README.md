# jobbot

A free, self-hosted job search bot that finds junior and associate software engineering roles shortly after they're posted. It ranks them against my skills, emails me a digest, and tracks where each application stands.

I built it because the best time to apply is in the first few hours after a posting goes live, before it spreads to LinkedIn and job aggregators. Paid tools that do this cost money and still lag behind. jobbot reads company job boards directly from their public ATS APIs, so it usually finds new roles within about an hour of publishing. It costs $0 to run.

## Features

- **Pulls straight from the source.** It fetches postings from the public Greenhouse and Ashby APIs for about 45 companies (fintech plus NYC tech), so nothing is scraped.
- **Filters for roles I'd actually qualify for.**
  - Titles are matched on word boundaries, so `intern` doesn't match "Internal Tools" and `Engineer I` doesn't match "Engineer in Test".
  - It excludes senior, staff, and lead roles, plus non-SWE "engineer" titles.
  - A years-of-experience parser reads descriptions ("3+ years", "2–4 years", "minimum of three years") and keeps roles in the 1–3 YOE range.
  - It catches new-grad postings hiding behind normal titles, like "graduating between Sep 2026 – Jul 2027".
  - Location filtering keeps NYC and US-remote roles and drops remote roles restricted to other countries.
- **Detects gaps.** Each posting is checked against a skills profile. It flags must-haves I can't claim, such as a required primary language I don't use, and drops roles that require security clearance.
- **Scores keyword fit.** Postings are ranked by keyword hits (Python, Go, PostgreSQL, Docker, payments, and so on), and title matches count more. The matched keywords appear in the digest, so I know which terms to echo in my resume.
- **Emails digests on a schedule.**
  - Hit-list companies get an immediate alert.
  - Everything else arrives in a 7am / 9pm ET HTML digest.
  - Each row shows the job's age, score, stated YOE, gaps, compensation (when listed), and a direct apply link.
  - Each row also has a generated LinkedIn search for recruiters at that company.
- **Tracks applications.**
  - A SQLite store dedupes postings, detects reposts, and records each one's status: `new → applied → reached_out → interviewing → rejected / skipped`.
  - `jobbot stats` reports weekly applications and response rate by company.
- **Ethical by design.** It uses only public, free data sources and never scrapes LinkedIn, Indeed, or Glassdoor. It doesn't auto-apply either. The tool finds and ranks jobs, and a human applies.

## How it works

```
GitHub Actions (hourly, ET quiet hours skipped)
        │
        ▼
  Greenhouse / Ashby APIs ──► normalize to a shared Job model
        │
        ▼
  filters (title → location → recency → YOE → new-grad → gaps)
        │
        ▼
  keyword scoring ──► SQLite (dedupe, reposts, status)
        │
        ▼
  dispatch: hit-list alert now, or 7am / 9pm ET digest ──► email (SMTP)
```

Design notes:

- **Lightweight fetching.** Greenhouse boards are listed without descriptions, which makes responses about 25× smaller. Full descriptions are fetched only for new jobs that pass the title and location filters.
- **Resilient runs.** One broken company board logs a warning and is skipped, so it never crashes the run.
- **Honest dates.** `posted_at` uses a true first-published date. When an API only exposes a "last updated" date, the field is left null rather than reporting misleading freshness.
- **Database on its own branch.** The database lives on a separate `data` branch, not `master`, so the code history stays small. The bot and the local CLI both push with `--force-with-lease`. If a push is rejected, they fetch the latest copy, re-apply their changes, and retry, so neither overwrites the other.
- **Timezone handling.** Cron runs in UTC, so the digest-window logic uses `zoneinfo` to handle daylight saving correctly.
- **Built-in backups.** A weekly workflow snapshots the database as an Actions artifact, kept for 90 days. A weekly `verify-companies` job reports any board that's broken, empty, or abandoned.

## Tech stack

Python 3.12 · httpx · pydantic · PyYAML · SQLite · smtplib · GitHub Actions · pytest · ruff · uv

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/dbueno3/job-app.git
cd job-app
uv sync
uv run pytest -q          # tests use recorded fixtures, never live endpoints
```

Edit the config to fit your own search:

- `config/companies.yaml`: companies to watch (`name`, `ats`, `slug`, `priority`, where `priority: 1` marks the hit list)
- `config/filters.yaml`: title include/exclude terms, YOE range, locations, scoring keywords
- `config/profile.yaml`: skills you can claim, skills you're learning, and skills you can't claim (used for gap detection)

Try a run without sending email:

```bash
uv run jobbot run --dry-run
```

To run it on GitHub Actions, add these repository secrets: `SMTP_USER`, `SMTP_APP_PASSWORD` (a Gmail app password), and `DIGEST_TO`.

## CLI

```bash
jobbot run [--dry-run] [--sync]      # fetch, filter, store, email
jobbot digest [--dry-run]            # send everything pending now
jobbot list --status new [--urls]    # list stored matches
jobbot status 12 applied --note "..."# update an application
jobbot stats                         # weekly applications, response rates
jobbot sync                          # pull data branch, replay local changes, push
jobbot verify-companies              # check every board is live and non-empty
```

## Tests

Over 150 pytest tests cover the source adapters, filters (including the YOE parser's edge cases), scoring, storage, data-branch sync, and the digest. They all run against trimmed JSON fixtures, never live APIs.

## Roadmap

- More ATS sources: Lever, Workable, SmartRecruiters
- Free feeds: SimplifyJobs GitHub lists, HN "Who's Hiring" via the Algolia API
