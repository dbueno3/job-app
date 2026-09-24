from datetime import datetime

import httpx
import pytest

from jobbot.config import Company, load_companies
from jobbot.sources import SOURCES, fetch_all
from jobbot.sources.ashby import AshbySource
from jobbot.sources.base import BoardNotFound, get_json, html_to_text
from jobbot.sources.greenhouse import GreenhouseSource
from tests.conftest import load_fixture, mock_client


def by_id(jobs):
    return {j.source_job_id: j for j in jobs}


# --- html_to_text -------------------------------------------------------------


def test_html_to_text_strips_tags_and_decodes_entities():
    html = "<h2>About</h2><p>We use <strong>Go</strong> &amp; Python.</p><ul><li>3+ years</li></ul>"
    assert html_to_text(html) == "About\n\nWe use Go & Python.\n\n3+ years"


def test_html_to_text_handles_nbsp_and_empty():
    assert html_to_text("a&nbsp;&nbsp;b") == "a b"
    assert html_to_text("") == ""


# --- Greenhouse ---------------------------------------------------------------


@pytest.fixture
def stripe_jobs():
    return by_id(GreenhouseSource().parse(load_fixture("greenhouse_stripe.json"), "Stripe"))


def test_greenhouse_parses_all_jobs(stripe_jobs):
    assert set(stripe_jobs) == {"8128744", "8127182", "8230952", "6692166", "8212508"}


def test_greenhouse_normalizes_fields(stripe_jobs):
    job = stripe_jobs["8128744"]
    assert job.source == "greenhouse"
    assert job.company == "Stripe"
    assert job.title == "Software Engineer, New Grad"
    assert job.location == "San Francisco, Seattle, New York"
    assert job.url == "https://stripe.com/jobs/search?gh_jid=8128744"
    assert isinstance(job.posted_at, datetime) and job.posted_at.tzinfo is not None


def test_greenhouse_description_is_plain_text(stripe_jobs):
    # Greenhouse double-escapes HTML; none of it should survive.
    for job in stripe_jobs.values():
        assert job.description_text
        assert "<" not in job.description_text
        assert "&lt;" not in job.description_text
        assert "&amp;" not in job.description_text


def test_greenhouse_remote_from_location_text(stripe_jobs):
    assert stripe_jobs["8127182"].is_remote  # "... US-Remote"
    assert stripe_jobs["6692166"].is_remote  # "Toronto, Canada Remote" (filters handle country)
    assert not stripe_jobs["8128744"].is_remote


def test_greenhouse_mercury_fixture():
    jobs = GreenhouseSource().parse(load_fixture("greenhouse_mercury.json"), "Mercury")
    assert len(jobs) == 2
    assert all(j.company == "Mercury" and j.is_remote for j in jobs)


def test_greenhouse_tolerates_missing_optional_fields():
    payload = {"jobs": [{"id": 1, "title": " SWE ", "absolute_url": "https://x/1"}]}
    [job] = GreenhouseSource().parse(payload, "X")
    assert job.title == "SWE"
    assert job.location == ""
    assert job.description_text == ""
    assert job.posted_at is None


# --- Ashby --------------------------------------------------------------------


@pytest.fixture
def ramp_jobs():
    return by_id(AshbySource().parse(load_fixture("ashby_ramp.json"), "Ramp"))


def test_ashby_parses_all_jobs(ramp_jobs):
    assert len(ramp_jobs) == 3


def test_ashby_normalizes_fields(ramp_jobs):
    job = ramp_jobs["bca0346c-b843-4795-96df-6091f51e421b"]
    assert job.source == "ashby"
    assert job.title == "Software Engineer, Data Platform"  # trailing space stripped
    assert job.location == "New York, NY (HQ)"
    assert job.url.startswith("https://jobs.ashbyhq.com/ramp/")
    assert job.posted_at is not None
    assert "<" not in job.description_text


def test_ashby_hybrid_is_not_remote_despite_isremote_flag(ramp_jobs):
    # Ramp marks Hybrid NYC-only roles isRemote=true; we shouldn't.
    assert not ramp_jobs["bca0346c-b843-4795-96df-6091f51e421b"].is_remote


def test_ashby_secondary_remote_location_counts(ramp_jobs):
    job = ramp_jobs["be496b52-cfbf-494e-b862-61fb4a188b24"]
    assert "Remote (US)" in job.location
    assert job.is_remote


def test_ashby_skips_unlisted_and_falls_back_to_html():
    payload = {
        "jobs": [
            {"id": "a", "title": "Hidden", "jobUrl": "u", "isListed": False},
            {"id": "b", "title": "Shown", "jobUrl": "u", "descriptionHtml": "<p>Hi</p>"},
        ]
    }
    [job] = AshbySource().parse(payload, "X")
    assert job.source_job_id == "b"
    assert job.description_text == "Hi"
    assert job.posted_at is None


# --- HTTP + error isolation -------------------------------------------------------


def test_get_json_404_raises_board_not_found():
    client = mock_client({})
    with pytest.raises(BoardNotFound):
        get_json(client, "https://api.example.com/missing", sleep=lambda s: None)


def test_get_json_retries_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    sleeps = []
    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert get_json(client, "https://api.example.com/x", sleep=sleeps.append) == {"ok": True}
    assert len(calls) == 3
    assert sleeps == [2.0, 4.0]  # exponential backoff


def test_get_json_gives_up_after_max_attempts():
    client = mock_client({"/x": httpx.ConnectError("boom")})
    with pytest.raises(httpx.ConnectError):
        get_json(client, "https://api.example.com/x", sleep=lambda s: None)


def test_fetch_all_skips_failing_boards(monkeypatch):
    monkeypatch.setattr("jobbot.sources.base.time.sleep", lambda s: None)
    client = mock_client(
        {
            "/v1/boards/stripe/jobs": httpx.Response(
                200, json=load_fixture("greenhouse_stripe.json")
            ),
            "/posting-api/job-board/ramp": httpx.Response(
                200, json=load_fixture("ashby_ramp.json")
            ),
            "/v1/boards/broken/jobs": httpx.Response(500),
            # "gone" is unrouted -> 404
        }
    )
    companies = [
        Company(name="Stripe", ats="greenhouse", slug="stripe"),
        Company(name="Gone", ats="greenhouse", slug="gone"),
        Company(name="Broken", ats="greenhouse", slug="broken"),
        Company(name="Weird", ats="workday", slug="weird"),
        Company(name="Ramp", ats="ashby", slug="ramp"),
    ]
    result = fetch_all(companies, client)
    assert result.counts == {"Stripe": 5, "Ramp": 3}
    assert len(result.jobs) == 8
    assert set(result.failures) == {"Gone", "Broken", "Weird"}
    assert "404" in result.failures["Gone"]


def test_seed_companies_config_is_valid():
    companies = load_companies()
    assert {"Stripe", "Brex", "Affirm", "Ramp", "Mercury"} <= {c.name for c in companies}
    assert all(c.ats in SOURCES for c in companies)
    boards = [(c.ats, c.slug) for c in companies]
    assert len(boards) == len(set(boards)), "duplicate board in companies.yaml"
