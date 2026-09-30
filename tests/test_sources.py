from datetime import datetime

import httpx
import pytest

from jobbot.cli import main
from jobbot.config import ConfigError, load_companies, load_filters, load_profile
from jobbot.models import Company
from jobbot.sources import fetch_all
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
    assert job.description_text is None  # list without content: fetched later
    assert job.posted_at is None


# --- Ashby --------------------------------------------------------------------


@pytest.fixture
def ramp_jobs():
    return by_id(AshbySource().parse(load_fixture("ashby_ramp.json"), "Ramp"))


def test_ashby_parses_all_jobs(ramp_jobs):
    assert len(ramp_jobs) == 4


def test_ashby_url_requests_compensation():
    assert AshbySource().board_url("ramp").endswith("/ramp?includeCompensation=true")


def test_ashby_compensation(ramp_jobs):
    assert ramp_jobs["bca0346c-b843-4795-96df-6091f51e421b"].compensation == (
        "$168K – $324.5K • Offers Equity"
    )
    # Company doesn't display pay on this posting, so neither do we.
    assert ramp_jobs["1515fe6d-1d8e-475b-a5ee-cefe43e78cb7"].compensation is None


def test_ashby_compensation_hidden_when_company_hides_it():
    raw = {
        "id": "x",
        "title": "SWE",
        "jobUrl": "u",
        "shouldDisplayCompensationOnJobPostings": False,
        "compensation": {"compensationTierSummary": "$100K – $150K"},
    }
    [job] = AshbySource().parse({"jobs": [raw]}, "X")
    assert job.compensation is None


def test_greenhouse_list_has_no_compensation(stripe_jobs):
    assert all(j.compensation is None for j in stripe_jobs.values())


# --- Greenhouse two-stage fetch ------------------------------------------------------


def test_greenhouse_list_url_has_no_content():
    url = GreenhouseSource().board_url("stripe")
    assert url == "https://boards-api.greenhouse.io/v1/boards/stripe/jobs"
    assert GreenhouseSource.needs_details and not AshbySource.needs_details


def test_greenhouse_list_without_content_leaves_description_unfetched():
    jobs = GreenhouseSource().parse(load_fixture("greenhouse_stripe_list.json"), "Stripe")
    assert len(jobs) == 4
    assert all(j.description_text is None for j in jobs)
    assert all(j.title and j.location and j.posted_at for j in jobs)  # enough for stage 1


def test_greenhouse_parse_details_single_range():
    desc, comp = GreenhouseSource().parse_details(load_fixture("greenhouse_job_yext.json"))
    assert comp == "$148.5K – $303.5K"
    assert desc.startswith("Yext") and "<" not in desc and "&lt;" not in desc


def test_greenhouse_parse_details_multiple_ranges():
    _, comp = GreenhouseSource().parse_details(load_fixture("greenhouse_job_toast.json"))
    assert comp == "$94K – $192K across 3 ranges"


@pytest.mark.parametrize(
    "ranges, expected",
    [
        ([], None),
        ([{"min_cents": 0, "max_cents": 0, "currency_type": "USD"}], None),
        ([{"min_cents": 12000000, "max_cents": 15000000, "currency_type": "USD"}], "$120K – $150K"),
        (
            [{"min_cents": 9000000, "max_cents": 11000000, "currency_type": "CAD"}],
            "CAD 90K – CAD 110K",
        ),
    ],
)
def test_greenhouse_compensation_format(ranges, expected):
    payload = {"content": "", "pay_input_ranges": ranges}
    assert GreenhouseSource().parse_details(payload)[1] == expected


def test_greenhouse_fetch_details_hits_per_job_endpoint():
    job = GreenhouseSource().parse(load_fixture("greenhouse_stripe_list.json"), "Stripe")[0]
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=load_fixture("greenhouse_job_yext.json"))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    filled = GreenhouseSource().fetch_details(client, "stripe", job)
    assert seen == [
        f"https://boards-api.greenhouse.io/v1/boards/stripe/jobs/{job.source_job_id}"
        "?pay_transparency=true"
    ]
    assert filled.description_text and filled.compensation == "$148.5K – $303.5K"
    assert filled.key == job.key and filled.title == job.title  # only details change


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
        Company(name="Weird", ats="lever", slug="weird"),  # no adapter yet
        Company(name="Ramp", ats="ashby", slug="ramp"),
    ]
    result = fetch_all(companies, client)
    assert result.counts == {"Stripe": 5, "Ramp": 4}
    assert len(result.jobs) == 9
    assert set(result.failures) == {"Gone", "Broken", "Weird"}
    assert "404" in result.failures["Gone"]


def test_fetch_all_pauses_between_boards():
    client = mock_client({})
    sleeps = []
    companies = [Company(name=n, ats="greenhouse", slug=n) for n in ("a", "b", "c")]
    fetch_all(companies, client, delay=0.5, sleep=sleeps.append)
    assert sleeps == [0.5, 0.5]  # between boards, not before the first


def test_fetch_all_lever_reports_missing_adapter():
    result = fetch_all([Company(name="Etsy", ats="lever", slug="etsy")], mock_client({}))
    assert "no adapter" in result.failures["Etsy"]


# --- companies.yaml validation ---------------------------------------------------


def write_yaml(tmp_path, body: str):
    path = tmp_path / "companies.yaml"
    path.write_text(body)
    return path


def test_real_configs_load():
    """My real config/ must parse (a typo like `priorty` would break the bot's run
    anyway). Nothing about its *contents* is asserted: any valid choice, like an
    empty hit list or a removed company, must never fail CI."""
    load_companies(include_disabled=True)
    load_filters()
    load_profile()


def test_company_defaults(tmp_path):
    path = write_yaml(tmp_path, "companies:\n  - {name: A, ats: ashby, slug: a}\n")
    [c] = load_companies(path)
    assert c.priority == 2 and c.enabled is True


def test_disabled_companies_are_skipped_unless_asked(tmp_path):
    path = write_yaml(
        tmp_path,
        "companies:\n"
        "  - {name: A, ats: ashby, slug: a}\n"
        "  - {name: B, ats: ashby, slug: b, enabled: false}\n",
    )
    assert [c.name for c in load_companies(path)] == ["A"]
    assert [c.name for c in load_companies(path, include_disabled=True)] == ["A", "B"]


@pytest.mark.parametrize(
    "entry, message",
    [
        ("{name: A, ats: workday, slug: a}", "'A'"),  # unknown ats
        ("{name: A, ats: ashby, slug: a, priority: 3}", "'A'"),  # priority must be 1 or 2
        ("{name: A, ats: ashby, slug: a, priorty: 1}", "priorty"),  # typo'd field
        ("{name: A, ats: ashby}", "slug"),  # missing slug
    ],
)
def test_invalid_company_fails_loudly(tmp_path, entry, message):
    path = write_yaml(tmp_path, f"companies:\n  - {entry}\n")
    with pytest.raises(ConfigError, match=message):
        load_companies(path)


def test_duplicate_board_fails_loudly(tmp_path):
    path = write_yaml(
        tmp_path,
        "companies:\n"
        "  - {name: Mercury, ats: greenhouse, slug: mercury}\n"
        "  - {name: Mercury again, ats: greenhouse, slug: mercury}\n"
        "  - {name: Mercury Ashby, ats: ashby, slug: mercury}\n",  # same slug, other ATS: fine
    )
    with pytest.raises(ConfigError, match="greenhouse/mercury"):
        load_companies(path)


# --- verify-companies --------------------------------------------------------------


def test_verify_companies_reports_three_cases(monkeypatch, capsys):
    companies = [
        Company(name="Stripe", ats="greenhouse", slug="stripe"),
        Company(name="Mercury", ats="ashby", slug="mercury"),
        Company(name="Gone", ats="greenhouse", slug="gone"),
    ]
    client = mock_client(
        {
            "/v1/boards/stripe/jobs": httpx.Response(
                200, json=load_fixture("greenhouse_stripe.json")
            ),
            "/posting-api/job-board/mercury": httpx.Response(200, json={"jobs": []}),
        }
    )
    monkeypatch.setattr("jobbot.cli.load_companies", lambda **kw: companies)
    monkeypatch.setattr("jobbot.cli.make_client", lambda: client)

    assert main(["verify-companies"]) == 1  # an error board makes it exit non-zero
    out = capsys.readouterr().out
    errors, rest = out.split("EMPTY")
    empty, ok = rest.split("OK")
    assert "Gone [greenhouse/gone]" in errors and "404" in errors
    assert "Mercury [ashby/mercury]" in empty
    assert "Stripe [greenhouse/stripe]: 5 jobs" in ok


def test_verify_companies_warns_when_no_hit_list(monkeypatch, capsys):
    routes = {"/v1/boards/stripe/jobs": httpx.Response(200, json={"jobs": []})}
    monkeypatch.setattr("jobbot.cli.make_client", lambda: mock_client(routes))  # fresh each run
    no_hits = [Company(name="Stripe", ats="greenhouse", slug="stripe")]
    monkeypatch.setattr("jobbot.cli.load_companies", lambda **kw: no_hits)
    assert main(["verify-companies"]) == 0  # a warning, never a failure
    assert "WARNING: no priority-1" in capsys.readouterr().out

    with_hit = [Company(name="Stripe", ats="greenhouse", slug="stripe", priority=1)]
    monkeypatch.setattr("jobbot.cli.load_companies", lambda **kw: with_hit)
    main(["verify-companies"])
    assert "WARNING" not in capsys.readouterr().out


def test_cli_reports_config_errors(monkeypatch, capsys):
    def bad(**kw):
        raise ConfigError("companies.yaml: duplicate (ats, slug): greenhouse/x")

    monkeypatch.setattr("jobbot.cli.load_companies", bad)
    assert main(["verify-companies"]) == 2
    assert "config error" in capsys.readouterr().out
