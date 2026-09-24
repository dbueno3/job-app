from datetime import UTC, datetime, timedelta

import pytest

from jobbot.config import load_filters
from jobbot.filters import (
    LocationMatcher,
    compile_terms,
    dedupe,
    is_recent,
    parse_min_yoe,
    title_ok,
    yoe_ok,
)
from jobbot.models import Job

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
CFG = load_filters()


def make_job(**overrides) -> Job:
    fields = dict(
        source="greenhouse",
        source_job_id="1",
        company="Acme",
        title="Software Engineer",
        location="New York, NY",
        is_remote=False,
        url="https://example.com/1",
        description_text="",
        posted_at=NOW - timedelta(days=1),
    )
    return Job(**(fields | overrides))


# --- years-of-experience parser ---------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        # plus forms
        ("3+ years of experience", 3),
        ("3 + years of software experience", 3),
        ("5+ yrs experience with Go", 5),
        ("2 plus years of professional experience", 2),
        ("3 or more years of experience", 3),
        # ranges -> lower bound
        ("2-4 years of experience", 2),
        ("2–4 years of experience", 2),  # en dash
        ("2 — 4 years of experience", 2),  # em dash with spaces
        ("2 to 4 years of experience", 2),
        ("2–12+ years of industry software engineering experience", 2),
        ("three to five years of experience", 3),
        # minimum / at least
        ("minimum of 3 years of experience", 3),
        ("Minimum 2 years in a software development role", 2),
        ("at least 4 years of backend engineering", 4),
        # word numbers, one through ten
        ("three years of experience", 3),
        ("one year of professional experience", 1),
        ("ten years of industry experience", 10),
        ("Five+ years building distributed systems", 5),
        # legal-style "three (3) years"
        ("Must also have three (3) years of experience in the following", 3),
        ("plus two (2) years of experience", 2),
        # decimals
        ("You have a total of 1.5+ years of experience as a software engineer", 1.5),
        # singular
        ("1 year experience working with Apache Spark", 1),
        # case
        ("3+ YEARS OF EXPERIENCE", 3),
    ],
)
def test_parse_min_yoe_forms(text, expected):
    assert parse_min_yoe(text) == expected


def test_parse_min_yoe_takes_lowest_minimum():
    text = "5+ years of experience in backend.\nOr 3+ years of experience with a master's degree."
    assert parse_min_yoe(text) == 3


def test_parse_min_yoe_lowest_across_ranges_and_plus():
    text = "- 4-6 years of software experience\n- 2+ years working with Kubernetes"
    assert parse_min_yoe(text) == 2


@pytest.mark.parametrize(
    "text",
    [
        "",
        "We are a fast-growing fintech company.",
        # company history / time horizons, not requirements
        "Over the past two years, we've significantly expanded our startup customers.",
        "Central to our plans for the next 5-10 years and building them well matters.",
        "In the last 3 years our engineering team has tripled.",
        "Founded 8 years ago, we build software.",
        # education and benefits
        "BA/BS or equivalent 4-year degree required",
        "We also have up to four weeks per year of fully remote work!",
        "Fertility HRA (up to $10,000 per year)",
        "Relevant PhD can count for up to 2 years of experience",
        # numbers not followed by years
        "3+ million ATMs worldwide",
        "Work with 5 teams on building software",
        # "years" with no experience context in the sentence
        "Build and ship systems in weeks that will last 5 years.",
        # implausibly large
        "Our team has 20+ years of combined experience",
    ],
)
def test_parse_min_yoe_ignores_non_requirements(text):
    assert parse_min_yoe(text) is None


def test_parse_min_yoe_ignores_false_hit_but_keeps_real_one():
    text = (
        "Over the past two years we grew 10x.\n"
        "Requirements:\n- 4+ years of experience building backend services"
    )
    assert parse_min_yoe(text) == 4


def test_parse_min_yoe_sentence_boundary_scopes_context():
    # "experience" is in the next bullet, not in the sentence with the number.
    text = "Build systems that will last 5 years\n- Strong experience with Go"
    assert parse_min_yoe(text) is None


@pytest.mark.parametrize(
    "min_yoe, keep",
    [(None, True), (0.5, True), (2, True), (3, True), (3.5, False), (5, False)],
)
def test_yoe_ok(min_yoe, keep):
    assert yoe_ok(min_yoe, max_yoe=3) is keep


# --- title --------------------------------------------------------------------

EXCLUDE = compile_terms(CFG.title_exclude)
INCLUDE = compile_terms(CFG.title_include)
REQUIRE = compile_terms(CFG.title_require)


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Software Engineer II",
        "Software Engineer, Backend",
        "Backend Engineer, Payments",
        "Software Engineer, New Grad",
        "Software Engineer, Early Career — Immediate Start",
        "Associate Software Developer",
        "Platform Engineer",
        "Infrastructure Engineer",
        "Engineer I, Data",
        "Software Engineer 2",
        "SWE, Growth",
        "Software Engineer, Internal Systems",  # "internal" is not "intern"
        "Software Engineer, Security, Stablecoin",  # not "security engineer"
        "Machine Learning Engineer II",
        "Full Stack Engineer, Web Presence and Platform",
        "Backend/API Engineer, Money as a Service",
    ],
)
def test_title_kept(title):
    assert title_ok(title, EXCLUDE, INCLUDE, REQUIRE)


@pytest.mark.parametrize(
    "title",
    [
        "Senior Software Engineer",
        "Sr. Software Engineer",
        "Sr Backend Engineer",
        "Staff Software Engineer, Payments",
        "Principal Engineer, Infrastructure",
        "Tech Lead, Platform",
        "Engineering Manager, Backend",
        "Director of Engineering",
        "Head of Platform",
        "Solutions Architect, Platform",
        "Software Engineer, Intern",
        "Software Engineering Internship, Backend",
        "Large User Sales Managers, Platform Partnerships",  # plural exclude term
        "Tech Leads, Backend",
        "Account Executive",  # no include term
        "Security Engineer",  # engineer, but none of the include terms
        # include term but no engineering word
        "Customer Experience Associate",
        "Operations Associate, New Grad (Mexico)",
        "Account Executive, Enterprise Platforms",
        "Data Scientist, Core Infrastructure",
        "Strategy & Operations, Infrastructure",
        # engineering word but not a software engineering role
        "Analytics Engineer II",
        "Product Security Engineer II",
        "Client Platform Security Engineer",
        "Solutions Engineer, Platform",
        "Sales Engineer, Infrastructure",
        "Systems Engineer, Corporate Infrastructure",
    ],
)
def test_title_dropped(title):
    assert not title_ok(title, EXCLUDE, INCLUDE, REQUIRE)


def test_engineer_i_does_not_match_inside_engineer_iii():
    include = compile_terms(["engineer i"])
    assert include.search("Software Engineer I")
    assert not include.search("Software Engineer III")


# --- location -----------------------------------------------------------------

LOC = LocationMatcher(CFG.locations)


@pytest.mark.parametrize(
    "location, is_remote",
    [
        ("New York, NY", False),
        ("New York, NY (HQ); San Francisco, CA", False),
        ("San Francisco, Seattle, NYC", False),
        ("Brooklyn, NY", False),
        ("US-Remote", True),
        ("Remote in the US", True),
        ("Remote (US)", True),
        ("Remote, United States", True),
        ("Seattle, WA OR Remote North America", True),
        ("Remote within Canada or United States", True),  # US is allowed
        ("Remote", True),  # no country named -> assume US-eligible
        ("Chicago, Atlanta, Remote", True),
    ],
)
def test_location_kept(location, is_remote):
    assert LOC.ok(make_job(location=location, is_remote=is_remote))


@pytest.mark.parametrize(
    "location, is_remote",
    [
        ("San Francisco, CA", False),
        ("Seattle, WA", False),
        ("London", False),
        ("Toronto, Canada Remote", True),
        ("Remote-Canada", True),
        ("London, Dublin, UK-Remote", True),
        ("Poland - Remote OR Romania - Remote", True),
        ("Remote (EMEA)", True),
    ],
)
def test_location_dropped(location, is_remote):
    assert not LOC.ok(make_job(location=location, is_remote=is_remote))


def test_location_ny_matches_as_word_only():
    # "ny" must not match inside other words like "Sunnyvale" or "company".
    assert not LOC.ok(make_job(location="Sunnyvale, CA"))


# --- recency ------------------------------------------------------------------


@pytest.mark.parametrize(
    "age_days, keep",
    [(0, True), (13, True), (14, True), (15, False), (90, False)],
)
def test_is_recent(age_days, keep):
    job = make_job(posted_at=NOW - timedelta(days=age_days))
    assert is_recent(job, NOW, max_age_days=14, keep_undated=True) is keep


def test_is_recent_undated_follows_setting():
    job = make_job(posted_at=None)
    assert is_recent(job, NOW, 14, keep_undated=True)
    assert not is_recent(job, NOW, 14, keep_undated=False)


# --- dedupe -------------------------------------------------------------------


def test_dedupe_collapses_reposts_keeping_newest():
    old = make_job(source_job_id="1", posted_at=NOW - timedelta(days=10))
    new = make_job(source_job_id="2", posted_at=NOW - timedelta(days=1))
    [kept] = dedupe([old, new])
    assert kept.source_job_id == "2"


def test_dedupe_ignores_case_and_whitespace():
    a = make_job(source_job_id="1", title="Software Engineer ")
    b = make_job(source_job_id="2", title="software  engineer")
    assert len(dedupe([a, b])) == 1


def test_dedupe_keeps_same_title_in_different_locations():
    a = make_job(source_job_id="1", location="New York, NY")
    b = make_job(source_job_id="2", location="US-Remote", is_remote=True)
    assert len(dedupe([a, b])) == 2


def test_dedupe_keeps_same_title_at_different_companies():
    a = make_job(source_job_id="1", company="Stripe")
    b = make_job(source_job_id="2", company="Brex")
    assert len(dedupe([a, b])) == 2


def test_dedupe_exact_repeat():
    job = make_job()
    assert len(dedupe([job, job.model_copy()])) == 1


def test_dedupe_prefers_dated_over_undated():
    undated = make_job(source_job_id="1", posted_at=None)
    dated = make_job(source_job_id="2")
    assert dedupe([undated, dated])[0].source_job_id == "2"
    assert dedupe([dated, undated])[0].source_job_id == "2"
