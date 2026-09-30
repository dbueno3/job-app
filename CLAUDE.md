# Job Search Bot — Project Spec

## Goal
A free, self-hosted Python tool that finds junior/associate software engineering roles
suited to an engineer with ~2 years of experience, ranks them, emails a digest of new
matches, and tracks application status. Runs on GitHub Actions on a schedule.

Freshness is a core goal: surface new postings within ~1 hour of publishing on the
company's own ATS, ahead of LinkedIn and aggregators. Early + qualified + tailored is
what gets interviews, so the tool also flags gaps and keywords to help tailoring.

## Hard constraints
- $0 to run. Only free, public data sources. No paid services.
- Do NOT scrape LinkedIn, Indeed, or Glassdoor. Use public ATS APIs and free feeds only.
- LinkedIn URLs are only *generated* for me to click. The tool never fetches them.
- Do NOT auto-submit applications. The tool discovers, ranks, and tracks; the human applies.
- Be polite to APIs: sequential requests or small concurrency, timeouts, retries with backoff,
  a small delay between requests, a descriptive User-Agent.

## Stack
- Python 3.12, `httpx` (HTTP), `pydantic` (models), `PyYAML` (config), `sqlite3` (stdlib storage)
- `zoneinfo` (stdlib) for America/New_York scheduling logic
- Email via `smtplib` using a Gmail app password stored in GitHub Secrets
- `pytest` with recorded JSON fixtures — tests must never hit live endpoints
- `ruff` for lint/format
- Ask before adding any dependency not listed here.

## Layout
```
jobbot/
  config/companies.yaml     # company name, ats type, board slug, priority, enabled
  config/filters.yaml       # title include/exclude, locations, max_yoe, keywords
  config/profile.yaml       # skills / learning / cannot_claim lists (for gap detection)
  jobbot/
    models.py               # Job + Company models
    config.py               # load + validate YAML configs
    sources/                # one adapter per source, shared interface
      base.py
      greenhouse.py
      ashby.py
      lever.py
    filters.py              # title, location, years-of-experience, must-have gaps
    scoring.py              # keyword fit score + matched keywords
    storage.py              # SQLite: dedupe, repost detection, status tracking
    datasync.py             # jobs.db on the `data` branch: fetch, replay, push with lease
    digest.py               # build + send HTML email
    links.py                # generated LinkedIn search URLs (never fetched)
    cli.py                  # run, digest, status, list, stats, sync, verify-companies
  tests/fixtures/
  .github/workflows/run.yml
  .github/workflows/verify.yml
```

## Config: companies.yaml
Fields per company: `name`, `ats` (greenhouse | ashby | lever), `slug`,
`priority` (1 = hit list, 2 = everyone else; default 2), `enabled` (default true).
- Validate with pydantic on load. Fail loudly on unknown `ats` values or duplicate
  `(ats, slug)` pairs.
- The YAML comments are my notes (e.g. why Mercury is on Greenhouse, not Ashby).
  Preserve them when editing the file.
- ~45 companies today. Expect this to grow; nothing should assume a small list.

`jobbot verify-companies`: fetch every enabled board and report three cases separately:
404 / error, 200 with zero jobs (an empty or abandoned board, like Mercury's Ashby board),
and OK with job count. Never auto-edit companies.yaml; just report.

## Job model (normalized across all sources)
`source, source_job_id, company, title, location, is_remote, url, description_text,
posted_at (nullable), first_seen_at, compensation (nullable text)`.
Unique key: `(source, source_job_id)`.
- Greenhouse `content` is HTML-escaped. Run `html.unescape` before stripping tags.
- Strip HTML from all descriptions before filtering.

## Sources — phase 1
Each adapter takes a board slug and returns `list[Job]`. A failing company logs a warning
and is skipped; one bad board must never crash the run.
- Greenhouse: list `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs` (no
  `content=true`; ~25x smaller), filter on title + location, then fetch descriptions
  only for jobs that pass and aren't in the DB, via
  `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?pay_transparency=true`
  (`pay_input_ranges` populates `compensation` when present).
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`.
  No lighter list option exists (tested includeDescription/fields/limit params: all
  ignored) and there is no public per-job endpoint (401), so boards are fetched whole
  (~0.3 MB gzipped each).
- Lever: `https://api.lever.co/v0/postings/{slug}?mode=json`

`posted_at`: verify which field each API exposes and whether it is a true first-published
date or a last-updated date. Document the answer in the adapter. Never use a last-updated
date as `posted_at`; leave it null instead.

Some Greenhouse boards return hundreds of jobs with full content. Fixtures must be trimmed
to a few representative jobs, never full live payloads.

## Filters (all configurable in filters.yaml)
All matching is case-insensitive and on word boundaries, never raw substrings
("intern" must not match "Internal Tools" or "International").
1. Title exclude (checked first): senior, sr, staff, principal, lead, manager, director,
   head of, architect, intern, internship; new-grad titles (new grad, early career,
   graduate, university/recent grad, apprentice); and non-SWE "engineer" roles
   (analytics, security, solutions, sales, support, systems, integration engineer, etc.).
2. Title include: software engineer, software developer, swe, backend, platform,
   infrastructure, associate, junior, engineer i, engineer ii, engineer 1, engineer 2.
   Keep "Software Engineer II", which is often the 2-YOE level.
   New-grad roles are deliberately NOT included: I want 1+ YOE roles, not roles hiring
   a specific graduation year.
   Generic terms (associate, platform, infrastructure, backend) only count when the title
   also has an engineering term (engineer, developer, swe). "Associate Product Manager"
   and "Platform Partnerships" must not pass. "Engineer I" must not match "Engineer in Test".
3. Years of experience: regex the description for requirements like "3+ years",
   "2-4 years", "minimum of 3 years", "three years". Handle word numbers (one through ten).
   Keep if min_yoe (1) <= min <= max_yoe (3), or if no requirement is found.
   min_yoe: 1 drops "0-2 years" roles.
   Current rule: take the lowest stated minimum. This is deliberately lenient
   (over-including beats missing roles), but "1+ year with Kafka" can mask "5+ years
   experience". Ask me before changing this rule.
   Write thorough unit tests for this parser, including the masking case.
4. New-grad descriptions: drop postings whose description names a graduation date
   ("graduating between Sep 2026 – Jul 2027", "degree obtained by summer 2027") or caps
   experience ("up to 18 months"), even with a normal title. Patterns need a year or a
   months cap so "mentor early-career engineers" doesn't match.
5. Location: keep New York / NYC / Brooklyn or remote-US. Drop remote roles restricted
   to other countries.
6. Recency: keep only jobs posted within `max_age_days` (14). Undated jobs are kept.
7. Must-have gaps (from profile.yaml): detect requirements I can't meet, such as security
   clearance, US citizenship requirements, and required primary languages not in my
   skills. Clearance → exclude. Everything else → keep but record in `gaps` and show
   in the digest. profile.yaml has three lists: `skills` (can claim), `learning`
   (counts toward score but never satisfies a must-have), `cannot_claim`.

## Scoring
Count keyword hits in title + description (Go, Python, Docker, Linux, Kubernetes,
Terraform, PostgreSQL, FastAPI, payments, fintech). Title hits weigh more.
Record which keywords matched. They go in the digest as terms to echo in my resume.

## Storage (SQLite, `jobs.db` on the `data` branch)
Table `jobs`: model fields + `matched` (bool), `score`, `matched_keywords`, `gaps`,
`is_repost`, `repost_of`, `status` (new | applied | reached_out | interviewing |
rejected | skipped), `notes`, `digested_at` (nullable), `updated_at`.
- Store metadata for every fetched job, descriptions only for jobs that passed
  title + location, so filter changes can be re-applied to history
  (`jobbot run --refilter`). If a loosened title/location filter makes old jobs
  eligible, `--refilter` fetches their missing descriptions on demand.
  Descriptions are stored compressed. Descriptions of unmatched jobs older than
  14 days are pruned.
- The DB lives on an orphan `data` branch as a single force-pushed commit, not on
  master (storing every job would grow the repo by ~15 MB per commit). Both the bot
  and `jobbot sync` push with `--force-with-lease`; on rejection they fetch the latest
  DB, re-apply their own pending changes, and retry, so neither side ever overwrites
  the other. Local status changes are journaled until a sync succeeds.
- Repost detection: a job is a likely repost if (company, normalized title, location)
  matches an existing row with a different `source_job_id`. Set `is_repost` and
  `repost_of`. If the earlier row's status is applied, reached_out, interviewing,
  rejected, or skipped, exclude it from the digest.
- CLI: `jobbot status <id> applied`, `jobbot list --status new`,
  `jobbot stats` (applications this week, counts by status, response rate by company).

## Digest email
HTML email of matched jobs where `digested_at` is null; set `digested_at` after a
successful send. Send nothing if there are no new matches.
- Priority-1 (hit list) matches are emailed immediately on the run that finds them.
- Everyone else goes in the 7am / 9pm America/New_York digest.
- Priority-1 (hit list) companies in their own section at the top, then everyone else.
- Within each section, sort by score, then age.
- Each row: company, title, location, age ("2h ago", or "first seen 2h ago" when
  `posted_at` is null), score, stated YOE, matched keywords, gaps, compensation if
  known, apply link (direct ATS/company link), and a LinkedIn search link for recruiters
  at that company:
  `https://www.linkedin.com/search/results/people/?keywords={company}%20technical%20recruiter`
- Footer: generated LinkedIn job-search links per location in filters.yaml:
  keywords "software engineer", `sortBy=DD`, `f_E=2,3` (entry + associate),
  `f_TPR` covering the time since the last digest. Verify these parameter codes.
- `--dry-run` prints the digest instead of sending.

## GitHub Actions
- `run.yml`: cron hourly (except 1am–5am ET, to save Actions minutes; my account has
  another private repo using them) plus `workflow_dispatch`. Every run fetches and
  stores, and emails any new priority-1 matches immediately.
  The regular digest is sent only on the first run at or after 7am and 9pm America/New_York.
  Cron is UTC, so do the time check in Python with `zoneinfo` so DST is handled.
  GitHub may delay scheduled runs, and that's acceptable.
- Push `jobs.db` to the `data` branch only if it changed. The repo is private because
  the DB contains my application statuses and notes.
- `verify.yml`: weekly `jobbot verify-companies`, output in the job log.
- Secrets: `SMTP_USER`, `SMTP_APP_PASSWORD`, `DIGEST_TO`.

## Build phases — complete and test each before starting the next
1. Models, config loading/validation, Greenhouse + Ashby adapters, verify-companies,
   fixtures, tests.
2. Filters (word-boundary matching, YOE parser, gaps) and scoring, with tests.
3. SQLite storage, dedupe, repost detection, CLI status/list/stats commands.
4. Email digest (sections, age, keywords, gaps, LinkedIn links) + `--dry-run`.
5. GitHub Actions workflows (hourly run with ET digest windows, weekly verify).
6. Later: Lever, Workable, SmartRecruiters, SimplifyJobs GitHub lists, HN "Who's Hiring"
   via the Algolia API, free-tier job APIs. Big tech with custom careers sites is out of
   scope until a free public API exists. No scrapers.

## Working style
Explain design decisions briefly as you go. I want to understand and own this code,
not just receive it. Flag tradeoffs and let me make the calls on anything non-obvious.