# Job Search Bot — Project Spec

## Goal
A free, self-hosted Python tool that finds junior/associate software engineering roles
suited to an engineer with ~2 years of experience, ranks them, emails a digest of new
matches, and tracks application status. Runs on GitHub Actions on a schedule.

## Hard constraints
- $0 to run. Only free, public data sources. No paid services.
- Do NOT scrape LinkedIn, Indeed, or Glassdoor. Use public ATS APIs and free feeds only.
- Do NOT auto-submit applications. The tool discovers, ranks, and tracks; the human applies.
- Be polite to APIs: sequential requests or small concurrency, timeouts, retries with backoff,
  a descriptive User-Agent.

## Stack
- Python 3.12, `httpx` (HTTP), `pydantic` (models), `PyYAML` (config), `sqlite3` (stdlib storage)
- Email via `smtplib` using a Gmail app password stored in GitHub Secrets
- `pytest` with recorded JSON fixtures — tests must never hit live endpoints
- `ruff` for lint/format
- Ask before adding any dependency not listed here.

## Layout
```
jobbot/
  config/companies.yaml     # company name, ats type, board slug
  config/filters.yaml       # title include/exclude, keywords, locations, max_yoe
  jobbot/
    models.py               # Job model
    sources/                # one adapter per source, shared interface
      base.py
      greenhouse.py
      ashby.py
      lever.py
    filters.py              # title, location, years-of-experience
    scoring.py              # keyword fit score
    storage.py              # SQLite: dedupe + status tracking
    digest.py               # build + send HTML email
    cli.py                  # entry points: run, status, add-company
  tests/fixtures/
  .github/workflows/run.yml
```

## Job model (normalized across all sources)
`source, source_job_id, company, title, location, is_remote, url, description_text,
posted_at (nullable), first_seen_at`. Unique key: `(source, source_job_id)`.
Strip HTML from descriptions before filtering.

## Sources — phase 1
Each adapter takes a board slug and returns `list[Job]`. A failing company logs a warning
and is skipped; one bad board must never crash the run.
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{slug}`
- Lever: `https://api.lever.co/v0/postings/{slug}?mode=json`

Seed `companies.yaml` with: Stripe, Brex, Affirm (Greenhouse); Ramp, Mercury (Ashby).
Verify each slug by fetching it; flag any that return 404.

## Filters (all configurable in filters.yaml)
1. Title exclude (checked first): senior, sr, staff, principal, lead, manager, director,
   head of, architect, intern.
2. Title include: software engineer, software developer, swe, backend, platform,
   infrastructure, associate, junior, early career, new grad, engineer i, engineer ii,
   engineer 1, engineer 2. Keep "Software Engineer II" — often the 2-YOE level.
3. Years of experience: regex the description for requirements like "3+ years",
   "2-4 years", "minimum of 3 years", "three years". Take the lowest stated minimum.
   Keep if min <= max_yoe (default 3) or if no requirement is found.
   Handle word numbers (one through ten). Write thorough unit tests for this parser.
4. Location: keep New York / NYC / Brooklyn or remote-US. Drop remote roles restricted
   to other countries.

## Scoring
Count keyword hits in title + description (Go, Python, Docker, Linux, Kubernetes,
Terraform, PostgreSQL, FastAPI, payments, fintech). Title hits weigh more.
Sort the digest by score, then recency.

## Storage (SQLite, `data/jobs.db`, committed back to the repo by the workflow)
Table `jobs`: model fields + `score`, `status` (new | applied | reached_out |
interviewing | rejected | skipped), `notes`, `updated_at`.
Only jobs not already in the DB go into the digest.
CLI: `jobbot status <id> applied`, `jobbot list --status new`.

## Digest email
HTML email, new matches only, sorted by score. Each row: company, title, location,
score, stated YOE, apply link, and a LinkedIn search link for recruiters at that company:
`https://www.linkedin.com/search/results/people/?keywords={company}%20technical%20recruiter`
Send nothing if there are no new matches.

## GitHub Actions
Cron every 6 hours plus `workflow_dispatch`. Install deps, run `jobbot run`,
commit `data/jobs.db` if it changed. Secrets: `SMTP_USER`, `SMTP_APP_PASSWORD`, `DIGEST_TO`.

## Build phases — complete and test each before starting the next
1. Models, Greenhouse + Ashby adapters, fixtures, tests.
2. Filters (especially the YOE parser) and scoring, with tests.
3. SQLite storage, dedupe, CLI status commands.
4. Email digest + a `--dry-run` flag that prints the digest instead of sending.
5. GitHub Actions workflow.
6. Later: Lever, Workable, SmartRecruiters, SimplifyJobs GitHub lists, HN "Who's Hiring"
   via the Algolia API, free-tier job APIs.

## Working style
Explain design decisions briefly as you go. I want to understand and own this code,
not just receive it — flag tradeoffs and let me make the calls on anything non-obvious.
