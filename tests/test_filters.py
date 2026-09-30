from datetime import UTC, datetime, timedelta

import pytest

from jobbot.config import Profile, load_filters
from jobbot.filters import (
    GapDetector,
    JobFilter,
    LocationMatcher,
    compile_terms,
    is_recent,
    parse_min_yoe,
    title_ok,
    yoe_ok,
)
from jobbot.models import Job
from tests.conftest import FIXTURE_CONFIG

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
CFG = load_filters(FIXTURE_CONFIG / "filters.yaml")


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


def test_parse_min_yoe_masking_case_is_lenient_by_design():
    # Known tradeoff (CLAUDE.md): the lowest stated minimum wins, so a narrow
    # "1+ year with Kafka" masks the real "5+ years" bar. Deliberate: over-including
    # beats missing roles. Don't change without asking.
    text = "- 5+ years of software engineering experience\n- 1+ year of experience with Kafka"
    assert parse_min_yoe(text) == 1


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
    [(None, True), (0, False), (0.5, False), (1, True), (2, True), (3, True), (3.5, False)],
)
def test_yoe_ok(min_yoe, keep):
    assert yoe_ok(min_yoe, max_yoe=3, floor=1) is keep


@pytest.mark.parametrize(
    "text, expected",
    [
        ("0-2 years of experience", 0),
        ("0+ years of professional experience", 0),
        ("0–3 years of software engineering experience, or 1+ years in a related role", 0),
    ],
)
def test_parse_min_yoe_zero(text, expected):
    assert parse_min_yoe(text) == expected


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
        "Software Engineer, New Grad",
        "Software Engineer, New Grad 2027",
        "Software Engineer, Early Career — Immediate Start",
        "Graduate Software Engineer",
        "University Grad Software Engineer",
        "Software Engineering Apprenticeship",
        "Software Engineer II, Android",
        "Software Engineer, iOS",
        "Mobile Software Engineer",
        "Software Engineer - Mobile Platform",
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


@pytest.mark.parametrize(
    "title",
    ["Associate Product Manager", "Platform Partnerships Lead", "Platform Partnerships"],
)
def test_generic_terms_need_an_engineering_word(title):
    assert not title_ok(title, EXCLUDE, INCLUDE, REQUIRE)


@pytest.mark.parametrize("title", ["International Payments Engineer", "Internal Tools Engineer"])
def test_intern_does_not_match_international_or_internal(title):
    assert not EXCLUDE.search(title)


def test_engineer_i_does_not_match_engineer_in_test():
    include = compile_terms(["engineer i"])
    assert not include.search("QA Engineer in Test")
    assert include.search("QA Engineer I")


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


# --- new-grad descriptions ------------------------------------------------------

JOB_FILTER = JobFilter(CFG)


@pytest.mark.parametrize(
    "text",
    [
        # real phrasing from live postings
        "Bachelor's degree in CS or a directly related field, obtained by summer 2027",
        "Current university student graduating between September 2026 – July 2027",
        "Recent graduates (including Summer 2026) or professionals with up to 18 months",
        "New grad candidates should have no more than 18 months of professional experience",
        "You are a college student graduating in the spring/summer of 2027",
        "with an expected graduation date of Winter 2027 or Spring 2028",
        "For this role, you need to graduate in December 2026",
        "Planning to graduate in 2028, all majors are welcome!",
        "Open to the Class of 2027",
    ],
)
def test_new_grad_description_detected(text):
    assert JOB_FILTER.is_new_grad(make_job(description_text=text))


@pytest.mark.parametrize(
    "text",
    [
        # real phrasing from regular (non-new-grad) postings
        "You'll also mentor early-career engineers to raise our collective quality bar",
        "Note: if you are an intern or new grad applicant, please do not apply using this link",
        "Manage a team of software engineers ranging from new grads to senior engineers",
        "Undergraduate or graduate degree in Computer Science, Engineering, or a related field",
        "5+ years professional working experience as a software engineer post graduation",
        "eliminate the class of problem",
        "3+ years of experience; we've grown 10x since 2021",
    ],
)
def test_new_grad_description_not_triggered(text):
    assert not JOB_FILTER.is_new_grad(make_job(description_text=text))


def test_evaluate_drops_new_grad_with_normal_title():
    job = make_job(
        title="Software Engineer 2",
        description_text="Recent graduates (including Summer 2026) or professionals with up to "
        "18 months of experience.",
    )
    assert JOB_FILTER.evaluate(job, NOW).reason == "new grad"


def test_evaluate_drops_below_min_yoe():
    job = make_job(description_text="0-2 years of software engineering experience")
    ev = JOB_FILTER.evaluate(job, NOW)
    assert (ev.reason, ev.min_yoe) == ("yoe", 0)


def test_evaluate_keeps_one_year():
    job = make_job(description_text="1+ years of software engineering experience")
    ev = JOB_FILTER.evaluate(job, NOW)
    assert (ev.reason, ev.min_yoe) == (None, 1)


# --- must-have gaps ---------------------------------------------------------------

PROFILE = Profile(
    skills=["Python", "SQL", "PostgreSQL", "FastAPI", "Docker", "Linux"],
    learning=["Go", "Terraform", "Kubernetes"],
    cannot_claim=["Java", "C++", "C#", "Scala"],
    us_citizen=True,
)
GAPS = GapDetector(PROFILE)


@pytest.mark.parametrize(
    "text, gaps",
    [
        # hard requirements for languages I can't claim
        ("6+ years of experience developing backend systems with Java and Spring", ["Java"]),
        ("Experience with Kotlin", ["Kotlin"]),
        ("Strong proficiency in C# and .NET", ["C#"]),
        # alternatives: one option I can claim is enough
        ("Experience with Go, Python, or Java", []),
        ("Proficient in at least one of Java, Go, Python, or TypeScript", []),
        ("Experience working with backend programming languages (Java, Kotlin, Python)", []),
        ("Experience with any part of our stack: React, TypeScript, Node.js, Postgres", []),
        # alternatives with no option I can claim
        ("Experience with C++ or Rust", ["one of C++ / Rust"]),
        (
            "Strong proficiency with Java, Kotlin, or other object-oriented languages",
            ["one of Java / Kotlin"],
        ),
        ("4+ years experience with a language such as Java or GO", ["one of Go (learning) / Java"]),
        # learning never satisfies a must-have
        ("5+ years of experience, with proficiency in Go", ["Go (learning)"]),
        # skills satisfy
        ("Strong experience with Python and PostgreSQL", []),
        # nice-to-haves and softened phrasing never create gaps
        ("Experience with Rust is a plus", []),
        ("Experience in latest versions of C++ highly desirable", []),
        ("Knowledge of or excitement to learn Haskell, and proficiency in SQL", []),
        ("(we don't expect experience in OCaml in particular.)", []),
        ("Familiarity with Go is helpful", []),
        # stack descriptions aren't requirements
        ("Some of the technologies we use include Go, PostgreSQL, Docker, Kafka", []),
        ("You'll own full-stack feature delivery across Golang and React", []),
        ("We work mostly in Java, Ruby, JavaScript, Scala, and Go.", []),
        # JavaScript is not Java
        ("Proficiency in JavaScript", ["JavaScript"]),
    ],
)
def test_language_gaps(text, gaps):
    assert GAPS.detect(text).gaps == gaps


def test_single_gap_dropped_when_one_of_gap_covers_it():
    text = "Strong proficiency with Java, Kotlin, or other languages.\n- Experience with Kotlin."
    assert GAPS.detect(text).gaps == ["one of Java / Kotlin"]


def test_learning_label_covered_too():
    text = "Experience with Go or TypeScript.\n- 5+ years of experience with Go."
    assert GAPS.detect(text).gaps == ["one of Go (learning) / TypeScript"]


def test_uncovered_single_gap_kept():
    text = "Experience with C++ or Rust.\n- Experience with Kotlin."
    assert GAPS.detect(text).gaps == ["one of C++ / Rust", "Kotlin"]


def test_gaps_deduped_across_sentences():
    text = "Experience with Kotlin.\n- Strong Kotlin skills required."
    assert GAPS.detect(text).gaps == ["Kotlin"]


def test_cannot_claim_entries_are_detected_even_if_not_in_catalog():
    gaps = GapDetector(Profile(cannot_claim=["COBOL"]))
    assert gaps.detect("Must have experience with COBOL").gaps == ["COBOL"]


@pytest.mark.parametrize(
    "text",
    [
        "Must hold an active security clearance",
        "TS/SCI clearance required",
        "Active Top Secret clearance with polygraph",
        "You currently hold a security clearance, or are eligible and willing to obtain one",
        "Clearance is required for this role",
    ],
)
def test_clearance_detected(text):
    assert GAPS.detect(text).clearance


@pytest.mark.parametrize(
    "text",
    [
        "TS/SCI clearance (preferred but not required).",
        "An active U.S. government security clearance is preferred",
        "Music Platform Clearances & Curation: contribute to weekly music clearances",
        "No security clearance required.",
    ],
)
def test_clearance_not_detected(text):
    assert not GAPS.detect(text).clearance


def test_citizenship_is_a_gap_only_for_non_citizens():
    text = "Must be a U.S. citizen due to government contract requirements."
    assert GAPS.detect(text).gaps == []  # profile: us_citizen true
    assert GapDetector(Profile(us_citizen=False)).detect(text).gaps == ["US citizenship"]


def test_eeo_boilerplate_is_not_a_citizenship_requirement():
    text = "We do not discriminate on the basis of race, national origin, citizenship, or age."
    assert GapDetector(Profile(us_citizen=False)).detect(text).gaps == []


def test_evaluate_excludes_clearance_and_records_gaps():
    job_filter = JobFilter(CFG, PROFILE)
    cleared = make_job(description_text="Must hold an active security clearance.")
    assert job_filter.evaluate(cleared, NOW).reason == "clearance"
    java = make_job(description_text="2+ years of experience with Java.")
    ev = job_filter.evaluate(java, NOW)
    assert (ev.reason, ev.min_yoe, ev.gaps) == (None, 2, ["Java"])
