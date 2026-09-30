import httpx
import pytest

from jobbot.config import load_filters, load_profile
from jobbot.models import Company
from jobbot.pipeline import DetailFetcher, Evaluator, ingest, refilter
from jobbot.sources import Listing
from jobbot.sources.ashby import AshbySource
from jobbot.sources.greenhouse import GreenhouseSource
from jobbot.storage import Storage
from tests.conftest import FIXTURE_CONFIG, load_fixture
from tests.test_filters import make_job

STRIPE = Company(name="Stripe", ats="greenhouse", slug="stripe")
RAMP = Company(name="Ramp", ats="ashby", slug="ramp")

# Recency off, so these tests don't depend on the clock.
CFG = load_filters(FIXTURE_CONFIG / "filters.yaml").model_copy(update={"max_age_days": 100_000})
PROFILE = load_profile(FIXTURE_CONFIG / "profile.yaml")

DETAIL_2YOE = {
    "content": "&lt;p&gt;2+ years of experience with Python and PostgreSQL.&lt;/p&gt;",
    "pay_input_ranges": [{"min_cents": 15000000, "max_cents": 19000000, "currency_type": "USD"}],
}


def listed(source_job_id: str, **fields):
    """A Greenhouse list entry: no description yet."""
    defaults = dict(
        source="greenhouse", company="Stripe", title="Software Engineer II", location="New York, NY"
    )
    return make_job(source_job_id=source_job_id, description_text=None, **(defaults | fields))


class DetailServer:
    """Mock Greenhouse per-job endpoint: returns `bodies[job_id]`, 500 for others."""

    def __init__(self, bodies: dict[str, dict]):
        self.bodies = bodies
        self.requested: list[str] = []

    def client(self) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            job_id = request.url.path.rsplit("/", 1)[-1]
            self.requested.append(job_id)
            if job_id in self.bodies:
                return httpx.Response(200, json=self.bodies[job_id])
            return httpx.Response(500)

        return httpx.Client(transport=httpx.MockTransport(handler))


def fixture_listings(*extra_greenhouse) -> list[Listing]:
    stripe = GreenhouseSource().parse(load_fixture("greenhouse_stripe_list.json"), "Stripe")
    ramp = AshbySource().parse(load_fixture("ashby_ramp.json"), "Ramp")
    return [Listing(STRIPE, stripe + list(extra_greenhouse)), Listing(RAMP, ramp)]


@pytest.fixture
def store():
    with Storage(":memory:") as s:
        yield s


def run_ingest(store, listings, server, cfg=CFG, details=None):
    details = details or DetailFetcher(server.client(), delay=0)
    return ingest(store, listings, Evaluator(cfg, PROFILE), details), details


def test_descriptions_fetched_only_for_title_location_passers(store):
    server = DetailServer({"9001": DETAIL_2YOE})
    result, details = run_ingest(store, fixture_listings(listed("9001")), server)

    assert server.requested == ["9001"]  # the 4 real Stripe jobs all fail stage 1
    assert result.new == 9 and details.requests == 1
    swe = store.get_by_key("greenhouse", "9001")
    assert swe.matched and swe.min_yoe == 2 and swe.job.compensation == "$150K – $190K"
    for sid in ("6692166", "8230952", "8212508", "8128744"):  # metadata only
        row = store.get_by_key("greenhouse", sid)
        assert row.job.description_text is None and not row.matched and row.drop_reason


def test_ashby_descriptions_kept_only_past_title_location(store):
    run_ingest(store, fixture_listings(), DetailServer({}))
    data_platform = store.get_by_key("ashby", "bca0346c-b843-4795-96df-6091f51e421b")
    account_exec = store.get_by_key("ashby", "1515fe6d-1d8e-475b-a5ee-cefe43e78cb7")
    assert data_platform.job.description_text
    assert account_exec.job.description_text is None and account_exec.drop_reason == "title"


def test_fixture_matches(store):
    run_ingest(store, fixture_listings(), DetailServer({}))
    matched = {(s.job.company, s.job.title) for s in store.list_jobs()}
    assert matched == {
        ("Ramp", "Software Engineer, Data Platform"),
        ("Ramp", "Software Engineer, Production Engineering"),
    }
    assert all(s.keywords for s in store.list_jobs())  # recorded for the digest


def test_ingest_is_idempotent(store):
    server = DetailServer({"9001": DETAIL_2YOE})
    run_ingest(store, fixture_listings(listed("9001")), server)
    again, details = run_ingest(store, fixture_listings(listed("9001")), server)
    assert again.new == 0 and details.requests == 0
    assert server.requested == ["9001"]


def test_replay_on_newer_db_uses_cached_details():
    server = DetailServer({"9001": DETAIL_2YOE})
    details = DetailFetcher(server.client(), delay=0)
    listings = fixture_listings(listed("9001"))
    with Storage(":memory:") as first:
        run_ingest(first, listings, server, details=details)
    with Storage(":memory:") as newer:  # e.g. the data branch moved; re-apply on top
        result, _ = run_ingest(newer, listings, server, details=details)
        assert result.new == 9 and newer.get_by_key("greenhouse", "9001").matched
    assert server.requested == ["9001"]  # fetched once across both applies


def test_within_run_duplicate_becomes_repost(store):
    server = DetailServer({"1": DETAIL_2YOE, "2": DETAIL_2YOE})
    result, _ = run_ingest(store, [Listing(STRIPE, [listed("1"), listed("2")])], server)
    assert result.reposts == 1
    assert store.get_by_key("greenhouse", "2").repost_of == store.get_by_key("greenhouse", "1").id


def test_failed_detail_fetch_keeps_metadata(store):
    result, details = run_ingest(store, [Listing(STRIPE, [listed("404")])], DetailServer({}))
    row = store.get_by_key("greenhouse", "404")
    assert details.failures == 1 and not row.matched and row.drop_reason == "no description"
    assert result.dropped == {"no description": 1}


def test_refilter_fetches_missing_descriptions_on_demand(store):
    senior = listed("7", title="Senior Software Engineer")
    server = DetailServer({"7": DETAIL_2YOE})
    run_ingest(store, [Listing(STRIPE, [senior])], server)
    assert server.requested == []  # "senior" fails stage 1, so no description yet

    loosened = CFG.model_copy(
        update={"title_exclude": [t for t in CFG.title_exclude if t != "senior"]}
    )
    details = DetailFetcher(server.client(), delay=0)
    result = refilter(store, [Listing(STRIPE, [senior])], Evaluator(loosened, PROFILE), details)
    assert server.requested == ["7"] and result.descriptions_fetched == 1
    assert result.newly_matched == 1
    row = store.get_by_key("greenhouse", "7")
    assert row.matched and row.job.description_text and row.digested_at is None


def test_refilter_uses_ashby_listing_description(store):
    listings = fixture_listings()
    run_ingest(store, listings, DetailServer({}))
    intern_id = "acf6b28d-767f-483f-8ff2-114620cd7e04"
    assert store.get_by_key("ashby", intern_id).job.description_text is None

    # "Software Engineering Intern" needs three loosenings: no intern exclude, and
    # include/require terms matching "engineering" (the defaults deliberately don't).
    loosened = CFG.model_copy(
        update={
            "title_exclude": [t for t in CFG.title_exclude if t not in ("intern", "internship")],
            "title_include": [*CFG.title_include, "software engineering"],
            "title_require": [*CFG.title_require, "engineering"],
        }
    )
    refilter(store, listings, Evaluator(loosened, PROFILE), DetailFetcher(None))
    assert store.get_by_key("ashby", intern_id).job.description_text  # from this run's listing


def test_refilter_can_unmatch_and_keeps_status(store):
    run_ingest(store, fixture_listings(), DetailServer({}))
    row = store.list_jobs()[0]
    store.set_status(row.id, "applied")
    stricter = CFG.model_copy(update={"title_exclude": [*CFG.title_exclude, "software"]})
    result = refilter(store, fixture_listings(), Evaluator(stricter, PROFILE), DetailFetcher(None))
    assert result.newly_unmatched == 2
    after = store.get(row.id)
    assert not after.matched and after.status == "applied"
