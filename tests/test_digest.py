from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import parse_qs, urlsplit

import pytest

from jobbot import cli, digest
from jobbot.config import LinkedInSearch, load_companies, load_filters, load_profile
from jobbot.digest import (
    DigestConfigError,
    SmtpConfig,
    build_alert,
    build_digest,
    format_age,
    group_rows,
    send_digest,
    split_title,
)
from jobbot.dispatch import (
    LAST_SENT,
    WINDOW_DONE,
    RunState,
    digest_due,
    footer_links,
    latest_window,
    next_window,
    plan_and_send,
    run_once,
)
from jobbot.links import job_search_url, recruiter_search_url
from jobbot.models import Company
from jobbot.pipeline import DetailFetcher, Evaluator
from jobbot.sources import FetchResult, Listing
from jobbot.storage import Storage
from tests.conftest import FIXTURE_CONFIG
from tests.test_filters import make_job
from tests.test_storage import match

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def utc(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


@pytest.fixture
def store():
    with Storage(":memory:") as s:
        yield s


def stored(store, *matches):
    """Insert matches and return them as the digest would see them (pending, sorted)."""
    for m in matches:
        store.insert(m.job, m, None, NOW)
    pending = store.pending_digest()
    store.mark_digested([p.job.key for p in pending], NOW)
    return pending


# --- links (generated, never fetched) ------------------------------------------------


def test_recruiter_search_url_encodes_company():
    assert recruiter_search_url("Stripe") == (
        "https://www.linkedin.com/search/results/people/?keywords=Stripe%20technical%20recruiter"
    )
    assert "keywords=AT%26T%20Labs%20technical%20recruiter" in recruiter_search_url("AT&T Labs")


def test_job_search_url_parameters():
    url = job_search_url("New York, New York, United States", 43200)
    parts = urlsplit(url)
    assert (parts.netloc, parts.path) == ("www.linkedin.com", "/jobs/search/")
    assert parse_qs(parts.query) == {
        "keywords": ["software engineer"],
        "location": ["New York, New York, United States"],
        "f_E": ["2,3"],  # entry + associate
        "f_TPR": ["r43200"],  # posted in the last 12h
        "sortBy": ["DD"],  # newest first
    }
    assert parse_qs(urlsplit(job_search_url("United States", 1, remote=True)).query)["f_WT"] == [
        "2"
    ]


@pytest.mark.parametrize("seconds, expected", [(10, "r3600"), (99_999_999, "r2592000")])
def test_job_search_window_is_clamped(seconds, expected):
    assert parse_qs(urlsplit(job_search_url("X", seconds)).query)["f_TPR"] == [expected]


# --- rows: fields, age, grouping ----------------------------------------------------


def test_digest_contains_every_required_field(store):
    jobs = stored(
        store,
        match(
            score=4, min_yoe=2, keywords=["Python", "payments"], gaps=["Java"],
            company="Affirm", title="Software Engineer II, Backend", location="Remote US",
            url="https://job-boards.greenhouse.io/affirm/jobs/1", compensation="$150K – $190K",
            posted_at=NOW - timedelta(hours=2),
        ),
    )  # fmt: skip
    d = build_digest([("New matches", jobs)], now=NOW, footer=[("New York", "https://li/ny")])
    for body in (d.html, d.text):
        for expected in (
            "Affirm", "Software Engineer II, Backend", "Remote US", "2h ago", "score 4",
            "2+ yrs", "Python, payments", "Java", "$150K – $190K",
            "https://job-boards.greenhouse.io/affirm/jobs/1",
            "keywords=Affirm%20technical%20recruiter", "https://li/ny",
        ):  # fmt: skip
            assert expected in body, expected


@pytest.mark.parametrize(
    "posted_ago, expected",
    [
        (timedelta(seconds=30), "just now"),
        (timedelta(minutes=45), "45m ago"),
        (timedelta(hours=2, minutes=59), "2h ago"),
        (timedelta(hours=47), "47h ago"),
        (timedelta(days=3, hours=5), "3d ago"),
    ],
)
def test_format_age(store, posted_ago, expected):
    [s] = stored(store, match(posted_at=NOW - posted_ago))
    assert format_age(s, NOW) == expected


def test_format_age_without_posted_date_uses_first_seen(store):
    [s] = stored(store, match(posted_at=None, first_seen_at=NOW - timedelta(hours=2)))
    assert format_age(s, NOW) == "first seen 2h ago"


def test_no_gaps_or_pay_lines_when_empty(store):
    d = build_digest([("New matches", stored(store, match()))], now=NOW)
    assert "gaps:" not in d.text and "gaps:" not in d.html and "$" not in d.text


def test_digest_subject_counts_postings(store):
    one = stored(store, match(source_job_id="1"))
    assert build_digest([("s", one)], now=NOW).subject == "jobbot: 1 new SWE match (Sep 24)"
    two = stored(
        store, match(source_job_id="2", title="A Engineer"), match(source_job_id="3", title="B")
    )
    assert build_digest([("s", two)], now=NOW).subject.startswith("jobbot: 2 new SWE matches")


def test_digest_escapes_html(store):
    jobs = stored(store, match(company="<script>Evil</script>", title="Eng & <b>Ops</b>"))
    html = build_digest([("s", jobs)], now=NOW).html
    assert "<script>" not in html
    assert "&lt;script&gt;Evil" in html and "Eng &amp; &lt;b&gt;Ops" in html


def test_sections_in_order_and_empty_ones_dropped(store):
    hit = stored(store, match(source_job_id="h", company="Ramp", title="Hit Engineer"))
    rest = stored(store, match(source_job_id="r", company="Acme", title="Rest Engineer"))
    d = build_digest([("Hit list", hit), ("New matches", rest)], now=NOW)
    assert (
        d.text.index("== Hit list ==")
        < d.text.index("Hit Engineer")
        < d.text.index("== New matches ==")
        < d.text.index("Rest Engineer")
    )
    only_rest = build_digest([("Hit list", []), ("New matches", rest)], now=NOW)
    assert "Hit list" not in only_rest.text


def test_split_title():
    assert split_title("Software Engineer, Agent - Retail") == (
        "Software Engineer, Agent",
        "Retail",
    )
    assert split_title("Software Engineer – Payments") == ("Software Engineer", "Payments")
    assert split_title("Software Engineer II") == ("Software Engineer II", None)
    assert split_title("Front-end Engineer") == ("Front-end Engineer", None)  # unspaced dash


SIERRA = ["Travel & Hospitality", "Retail", "Healthcare", "Insurance"]


def test_same_company_variants_group_into_one_row(store):
    jobs = stored(
        store,
        *[
            match(
                score=2,
                source_job_id=str(i),
                company="Sierra",
                title=f"Software Engineer, Agent - {v}",
                min_yoe=None if i else 2.0,
                keywords=["Go"] if i else ["Python"],
                gaps=["TypeScript"],
            )
            for i, v in enumerate(SIERRA)
        ],  # fmt: skip
        match(score=1, source_job_id="n", company="Notion", title="Software Engineer - AI"),
    )
    rows = group_rows(jobs)
    assert [len(r.jobs) for r in rows] == [4, 1]
    sierra = rows[0]
    assert sierra.title == "Software Engineer, Agent" and sierra.min_yoe == 2.0
    assert [name for name, _ in sierra.variants] == SIERRA
    assert sierra.keywords == ["Python", "Go"] and sierra.gaps == ["TypeScript"]  # merged
    assert rows[1].title == "Software Engineer - AI"  # a single job keeps its full title

    d = build_digest([("New matches", jobs)], now=NOW)
    assert d.subject.startswith("jobbot: 5 new SWE matches")  # counts postings, not rows
    assert "(4 variants)" in d.html and "(4 variants)" in d.text
    for v in SIERRA:
        assert f">{v.replace('&', '&amp;')}</a>" in d.html  # each variant is its own link
        assert f"- {v}: " in d.text
    assert d.html.count("<tr>") == 2


def test_same_base_title_at_different_companies_not_grouped(store):
    jobs = stored(
        store,
        match(source_job_id="1", company="A", title="Software Engineer - Payments"),
        match(source_job_id="2", company="B", title="Software Engineer - Payments"),
    )
    assert len(group_rows(jobs)) == 2


def test_alert_subjects(store):
    one = stored(store, match(source_job_id="1", company="Ramp", title="Software Engineer II"))
    assert build_alert(one, NOW).subject == "jobbot hit list: Ramp — Software Engineer II"
    two = stored(
        store,
        match(source_job_id="2", company="Ramp", title="Platform Engineer"),
        match(source_job_id="3", company="Stripe", title="Backend Engineer"),
    )
    alert = build_alert(two, NOW)
    assert alert.subject == "jobbot hit list: 2 new at Ramp, Stripe"
    assert "== Hit list ==" in alert.text and "LinkedIn" not in alert.text  # no footer


# --- ET digest windows (zoneinfo handles DST) -----------------------------------------


@pytest.mark.parametrize(
    "now, window",
    [
        # EDT (UTC-4): 7am = 11:00Z, 9pm = 01:00Z next day
        ("2026-09-30T11:30:00", "2026-09-30T11:00:00"),  # 7:30am -> today's 7am
        ("2026-09-30T10:59:00", "2026-09-30T01:00:00"),  # 6:59am -> last night's 9pm
        ("2026-10-01T00:59:00", "2026-09-30T11:00:00"),  # 8:59pm -> this morning's 7am
        ("2026-10-01T01:00:00", "2026-10-01T01:00:00"),  # exactly 9pm
        # EST (UTC-5): 7am = 12:00Z
        ("2026-12-01T12:30:00", "2026-12-01T12:00:00"),
        ("2026-12-01T11:59:00", "2026-12-01T02:00:00"),
        # DST ends Sun Nov 1 2026 (2am EDT -> 1am EST): 7am that day is EST
        ("2026-11-01T12:05:00", "2026-11-01T12:00:00"),
        # DST starts Sun Mar 8 2026 (2am EST -> 3am EDT): 7am that day is EDT
        ("2026-03-08T11:05:00", "2026-03-08T11:00:00"),
    ],
)
def test_latest_window(now, window):
    assert latest_window(utc(now)) == utc(window)


def test_next_window():
    assert next_window(utc("2026-09-30T11:30:00")) == utc("2026-10-01T01:00:00")  # 9pm EDT


def test_digest_due():
    now = utc("2026-09-30T11:30:00")  # 7:30am EDT
    assert digest_due(now, None)  # never handled
    assert digest_due(now, utc("2026-09-30T01:00:00"))  # last night's window handled only
    assert not digest_due(now, utc("2026-09-30T11:00:00"))  # this morning's handled


def test_footer_window_is_time_since_last_digest():
    search = [LinkedInSearch(label="NY", location="New York")]
    [(_, url)] = footer_links(search, NOW, NOW - timedelta(hours=10))
    assert "f_TPR=r36000" in url
    [(_, url)] = footer_links(search, NOW, None)  # no digest yet: default 12h
    assert "f_TPR=r43200" in url


# --- dispatch: hit-list alerts now, everyone else at 7am / 9pm ET -----------------------

MORNING = utc("2026-09-30T11:30:00")  # 7:30am EDT: the 7am window is due
MIDDAY = utc("2026-09-30T16:00:00")  # noon EDT: between windows


# Fixture config, not config/companies.yaml: the alert path is always exercised, and
# whatever I set (or clear) in my real hit list can never fail CI.
FIXTURE_COMPANIES = load_companies(FIXTURE_CONFIG / "companies.yaml", include_disabled=True)
PRIORITIES = {c.name: c.priority for c in FIXTURE_COMPANIES}


def _hit_list_company() -> Company:
    return next(c for c in FIXTURE_COMPANIES if c.priority == 1 and c.enabled)


def _pending(store, companies_titles):
    for i, (company, title) in enumerate(companies_titles):
        m = match(source_job_id=str(i), company=company, title=title)
        store.insert(m.job, m, None, MORNING - timedelta(hours=1))


def test_hit_list_company_is_emailed_immediately_between_windows(store):
    hit = _hit_list_company()
    other = next(c for c in FIXTURE_COMPANIES if c.priority == 2)
    _pending(store, [(hit.name, "Software Engineer II"), (other.name, "Backend Engineer")])
    store.set_meta(WINDOW_DONE, latest_window(MIDDAY).isoformat())  # 7am already handled

    sent = []
    plan = plan_and_send(store, sent.append, PRIORITIES, [], MIDDAY)

    assert [d.subject for d in sent] == [f"jobbot hit list: {hit.name} — Software Engineer II"]
    assert plan.alerts == 1 and plan.digest == 0
    assert plan.next_digest == next_window(MIDDAY)  # the other company waits for 9pm


def test_rest_goes_out_in_the_window_without_the_hit_list(store):
    priorities = {"Ramp": 1, "Acme": 2}
    _pending(store, [("Ramp", "Software Engineer II"), ("Acme", "Backend Engineer")])
    sent = []
    plan = plan_and_send(store, sent.append, priorities, [], MORNING)
    alert, regular = sent
    assert alert.subject.startswith("jobbot hit list: Ramp")
    assert regular.subject.startswith("jobbot: 1 new SWE match")
    assert "Acme" in regular.text and "Ramp" not in regular.text  # never sent twice
    assert plan.window_done == latest_window(MORNING) and plan.digest_sent_at == MORNING


def test_window_is_handled_once_even_with_nothing_to_send(store):
    sent = []
    plan = plan_and_send(store, sent.append, {}, [], MORNING)
    assert sent == [] and plan.window_done == latest_window(MORNING)
    assert plan.digest_sent_at is None  # footer keeps counting from the last real digest


def test_force_sends_everything_now_with_hit_list_on_top(store):
    _pending(store, [("Acme", "Backend Engineer"), ("Ramp", "Software Engineer II")])
    store.set_meta(WINDOW_DONE, latest_window(MIDDAY).isoformat())
    sent = []
    plan_and_send(store, sent.append, {"Ramp": 1}, [], MIDDAY, force=True)
    [d] = sent
    assert d.text.index("== Hit list ==") < d.text.index("Ramp") < d.text.index("== New matches")


# --- run_once: end to end on a DB file ----------------------------------------------

EVALUATOR = Evaluator(
    load_filters(FIXTURE_CONFIG / "filters.yaml").model_copy(update={"max_age_days": 100_000}),
    load_profile(FIXTURE_CONFIG / "profile.yaml"),
)
ACME = Company(name="Acme", ats="greenhouse", slug="acme")


def listing(*ids: str, company: Company = ACME) -> list[Listing]:
    jobs = [
        make_job(
            source_job_id=i, company=company.name, title=f"Software Engineer {i}",
            description_text="Python.",
        )
        for i in ids
    ]  # fmt: skip
    return [Listing(company, jobs)]


def run(db, lst, sent, state=None, now=MORNING, **kw):
    run_once(
        db, lst, EVALUATOR, DetailFetcher(None), state or RunState(), sent.append, now=now, **kw
    )


def test_run_once_digest_twice_a_day(tmp_path):
    db, sent = tmp_path / "jobs.db", []
    run(db, listing("1"), sent, now=MORNING)
    run(db, listing("1", "2"), sent, now=MIDDAY)  # found at noon: held
    run(db, listing("1", "2"), sent, now=utc("2026-10-01T01:10:00"))  # 9:10pm: sent
    assert [d.subject for d in sent] == [
        "jobbot: 1 new SWE match (Sep 30)",
        "jobbot: 1 new SWE match (Oct 1)",
    ]
    with Storage(db) as s:
        assert s.get_meta(WINDOW_DONE) == utc("2026-10-01T01:00:00").isoformat()
        assert s.get_meta(LAST_SENT) == utc("2026-10-01T01:10:00").isoformat()


def test_hit_list_company_run_once_sends_immediately(tmp_path):
    hit = _hit_list_company()
    db, sent = tmp_path / "jobs.db", []
    run(db, [], sent, now=MORNING)  # handle the 7am window first
    run(db, listing("h", company=hit), sent, now=MIDDAY, priorities=PRIORITIES)
    [alert] = sent
    assert alert.subject == f"jobbot hit list: {hit.name} — Software Engineer h"


def test_failed_send_leaves_jobs_pending(tmp_path):
    db = tmp_path / "jobs.db"

    def boom(d):
        raise OSError("smtp down")

    with pytest.raises(OSError):
        run_once(db, listing("1"), EVALUATOR, DetailFetcher(None), RunState(), boom, now=MORNING)
    sent = []
    run(db, listing("1"), sent)
    assert "Software Engineer 1" in sent[0].text  # retried next run


def test_retry_on_newer_db_marks_emailed_without_resending(tmp_path):
    state, sent = RunState(), []
    run(tmp_path / "a.db", listing("1"), sent, state=state)
    # Push rejected: apply again on another copy of the DB. No second email.
    run(tmp_path / "b.db", listing("1"), sent, state=state)
    assert len(sent) == 1
    with Storage(tmp_path / "b.db") as s:
        assert s.pending_digest() == []
        assert s.get_meta(WINDOW_DONE) is not None


def test_dry_run_writes_nothing(tmp_path):
    db, sent = tmp_path / "jobs.db", []
    run(db, listing("1"), sent, dry_run=True)
    assert len(sent) == 1
    with Storage(db) as s:
        assert s.counts()["jobs"] == 0 and s.get_meta(WINDOW_DONE) is None


# --- SMTP ----------------------------------------------------------------------------


def test_smtp_config_from_env(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcd efgh ijkl mnop")  # as Google displays it
    monkeypatch.setenv("DIGEST_TO", "inbox@example.com")
    assert SmtpConfig.from_env() == SmtpConfig(
        user="me@gmail.com", password="abcdefghijklmnop", to="inbox@example.com"
    )


def test_smtp_config_defaults_recipient_to_sender(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "pw")
    monkeypatch.delenv("DIGEST_TO", raising=False)
    assert SmtpConfig.from_env().to == "me@gmail.com"


def test_smtp_config_missing(monkeypatch):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    with pytest.raises(DigestConfigError):
        SmtpConfig.from_env()


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.timeout = host, port, timeout
        self.logged_in = None
        self.sent: list[EmailMessage] = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, msg):
        self.sent.append(msg)


def test_send_digest_builds_multipart_email(monkeypatch, store):
    FakeSMTP.instances = []
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    d = build_digest([("New matches", stored(store, match()))], now=NOW)
    send_digest(d, SmtpConfig(user="me@gmail.com", password="pw", to="me@gmail.com"))
    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
    assert smtp.logged_in == ("me@gmail.com", "pw")
    [msg] = smtp.sent
    assert msg["Subject"] == d.subject and msg["To"] == "me@gmail.com"
    assert msg.get_body(("plain",)).get_content().strip() == d.text.strip()
    assert "<table" in msg.get_body(("html",)).get_content()


# --- CLI (network mocked out) ---------------------------------------------------------


@pytest.fixture
def fake_fetch(monkeypatch):
    jobs = [
        make_job(
            source_job_id="1", title="Software Engineer II", company="Affirm",
            description_text="2+ years of Python.", posted_at=datetime.now(UTC),
        )
    ]  # fmt: skip
    company = Company(name="Affirm", ats="greenhouse", slug="affirm")
    result = FetchResult(jobs=jobs, listings=[Listing(company, jobs)])
    monkeypatch.setattr(cli, "fetch_all", lambda companies, client: result)


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "pw")


def test_run_without_smtp_env_fails_fast(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "fetch_all", lambda *a: pytest.fail("should not fetch"))
    assert cli.main(["--db", str(tmp_path / "j.db"), "run"]) == 2
    assert "SMTP_USER" in capsys.readouterr().out


def test_run_dry_run_prints_and_writes_nothing(fake_fetch, tmp_path, capsys):
    db, html = tmp_path / "j.db", tmp_path / "digest.html"
    assert cli.main(["--db", str(db), "run", "--dry-run", "--html", str(html)]) == 0
    out = capsys.readouterr().out
    assert "dry run: nothing saved, sent, or pushed" in out
    assert not db.exists()


def test_digest_command_sends_pending_now_then_nothing(fake_fetch, smtp, tmp_path, capsys):
    db = tmp_path / "j.db"
    with Storage(db) as s:  # something pending that the window logic would hold
        m = match(source_job_id="9", company="Acme", title="Backend Engineer")
        s.insert(m.job, m, None)
        s.set_meta(WINDOW_DONE, latest_window(datetime.now(UTC)).isoformat())
        s.commit()
    assert cli.main(["--db", str(db), "digest"]) == 0
    assert "sent to me@gmail.com: jobbot: 1 new SWE match" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "digest"]) == 0
    assert "no new matches; nothing sent" in capsys.readouterr().out
    assert len(FakeSMTP.instances) == 1


def test_empty_hit_list_is_valid_and_never_blocks_a_run(store):
    """Clearing every priority-1 flag is a valid choice: everything goes to the digest."""
    _pending(store, [("Ramp", "Software Engineer II")])
    sent = []
    plan_and_send(store, sent.append, {}, [], MORNING)
    [d] = sent
    assert d.subject.startswith("jobbot: 1 new SWE match")
