from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from jobbot import cli, digest
from jobbot.cli import RunState, run_once
from jobbot.config import load_filters, load_profile
from jobbot.digest import (
    DigestConfigError,
    SmtpConfig,
    build_digest,
    group_rows,
    recruiter_search_url,
    send_digest,
    split_title,
)
from jobbot.models import Company
from jobbot.pipeline import DetailFetcher, Evaluator
from jobbot.sources import FetchResult, Listing
from jobbot.storage import Storage
from tests.test_filters import make_job
from tests.test_storage import match

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


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


# --- building -----------------------------------------------------------------


def test_recruiter_search_url_encodes_company():
    assert recruiter_search_url("Stripe") == (
        "https://www.linkedin.com/search/results/people/?keywords=Stripe%20technical%20recruiter"
    )
    assert "keywords=AT%26T%20Labs%20technical%20recruiter" in recruiter_search_url("AT&T Labs")


def test_digest_contains_every_required_field(store):
    jobs = stored(
        store,
        match(
            score=4,
            min_yoe=2,
            company="Affirm",
            title="Software Engineer II, Backend",
            location="Remote US",
            url="https://job-boards.greenhouse.io/affirm/jobs/1",
        ),
    )
    d = build_digest(jobs, now=NOW)
    for body in (d.html, d.text):
        assert "Affirm" in body
        assert "Software Engineer II, Backend" in body
        assert "Remote US" in body
        assert "2+ yrs" in body
        assert "https://job-boards.greenhouse.io/affirm/jobs/1" in body
        assert "keywords=Affirm%20technical%20recruiter" in body
    assert ">4</td>" in d.html  # score column
    assert "score 4" in d.text


def test_digest_subject_pluralization(store):
    one = stored(store, match(source_job_id="1"))
    assert build_digest(one, now=NOW).subject == "jobbot: 1 new SWE match (Sep 24)"
    two = stored(
        store,
        match(source_job_id="2", title="A Engineer"),
        match(source_job_id="3", title="B Engineer"),
    )
    assert build_digest(two, now=NOW).subject.startswith("jobbot: 2 new SWE matches")


def test_digest_unstated_yoe(store):
    d = build_digest(stored(store, match(min_yoe=None)), now=NOW)
    assert "not stated" in d.text


def test_digest_escapes_html(store):
    jobs = stored(store, match(company="<script>Evil</script>", title="Eng & <b>Ops</b>"))
    html = build_digest(jobs, now=NOW).html
    assert "<script>" not in html
    assert "&lt;script&gt;Evil" in html
    assert "Eng &amp; &lt;b&gt;Ops" in html


def test_digest_preserves_order(store):
    jobs = stored(
        store,
        match(score=9, source_job_id="a", title="First Engineer"),
        match(score=1, source_job_id="b", title="Second Engineer"),
    )
    text = build_digest(jobs, now=NOW).text
    assert text.index("First Engineer") < text.index("Second Engineer")


# --- SMTP config and sending -----------------------------------------------------


def test_smtp_config_from_env(monkeypatch):
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "abcd efgh ijkl mnop")  # as Google displays it
    monkeypatch.setenv("DIGEST_TO", "inbox@example.com")
    cfg = SmtpConfig.from_env()
    assert cfg == SmtpConfig(
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
    d = build_digest(stored(store, match()), now=NOW)
    send_digest(d, SmtpConfig(user="me@gmail.com", password="pw", to="me@gmail.com"))

    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("smtp.gmail.com", 465)
    assert smtp.logged_in == ("me@gmail.com", "pw")
    [msg] = smtp.sent
    assert msg["Subject"] == d.subject
    assert msg["To"] == "me@gmail.com"
    assert msg.get_body(("plain",)).get_content().strip() == d.text.strip()
    assert "<table" in msg.get_body(("html",)).get_content()


# --- grouping variants ------------------------------------------------------------


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
            )
            for i, v in enumerate(SIERRA)
        ],  # fmt: skip
        match(score=1, source_job_id="n", company="Notion", title="Software Engineer - AI"),
    )
    rows = group_rows(jobs)
    assert [len(r.jobs) for r in rows] == [4, 1]
    assert rows[0].title == "Software Engineer, Agent" and rows[0].min_yoe == 2.0
    assert [name for name, _ in rows[0].variants] == SIERRA
    assert rows[1].title == "Software Engineer - AI"  # a single job keeps its full title

    d = build_digest(jobs, now=NOW)
    assert d.subject.startswith("jobbot: 5 new SWE matches")  # counts postings
    assert "(4 variants)" in d.html and "(4 variants)" in d.text
    for v in SIERRA:
        assert f">{v.replace('&', '&amp;')}</a>" in d.html  # each variant is its own link
        assert f"- {v}: " in d.text
    assert d.html.count("<tr>") == 3  # header + 2 rows


def test_same_base_title_at_different_companies_not_grouped(store):
    jobs = stored(
        store,
        match(source_job_id="1", company="A", title="Software Engineer - Payments"),
        match(source_job_id="2", company="B", title="Software Engineer - Payments"),
    )
    assert len(group_rows(jobs)) == 2


# --- run_once: ingest -> digest -> mark digested --------------------------------------

EVALUATOR = Evaluator(load_filters().model_copy(update={"max_age_days": 100_000}), load_profile())
ACME = Company(name="Acme", ats="greenhouse", slug="acme")


def listing(*ids: str) -> list[Listing]:
    jobs = [
        make_job(source_job_id=i, title=f"Software Engineer {i}", description_text="Python.")
        for i in ids
    ]
    return [Listing(ACME, jobs)]


def test_run_once_emails_new_matches_once(tmp_path):
    db, sent = tmp_path / "jobs.db", []
    run_once(db, listing("1", "2"), EVALUATOR, DetailFetcher(None), RunState(), sent.append)
    run_once(db, listing("1", "2", "3"), EVALUATOR, DetailFetcher(None), RunState(), sent.append)
    assert [[s.job.source_job_id for s in batch] for batch in sent] == [["1", "2"], ["3"]]


def test_failed_send_leaves_jobs_pending(tmp_path):
    db = tmp_path / "jobs.db"

    def boom(pending):
        raise OSError("smtp down")

    with pytest.raises(OSError):
        run_once(db, listing("1"), EVALUATOR, DetailFetcher(None), RunState(), boom)
    sent = []
    run_once(db, listing("1"), EVALUATOR, DetailFetcher(None), RunState(), sent.append)
    assert [s.job.source_job_id for s in sent[0]] == ["1"]  # retried next run


def test_retry_on_newer_db_marks_digested_without_resending(tmp_path):
    state, sent = RunState(), []
    run_once(tmp_path / "a.db", listing("1"), EVALUATOR, DetailFetcher(None), state, sent.append)
    # Push rejected: apply again on another copy of the DB. No second email.
    run_once(tmp_path / "b.db", listing("1"), EVALUATOR, DetailFetcher(None), state, sent.append)
    assert len(sent) == 1
    with Storage(tmp_path / "b.db") as s:
        assert s.pending_digest() == []


def test_dry_run_writes_nothing(tmp_path):
    db, sent = tmp_path / "jobs.db", []
    run_once(
        db, listing("1"), EVALUATOR, DetailFetcher(None), RunState(), sent.append, dry_run=True
    )
    assert len(sent) == 1
    with Storage(db) as s:
        assert s.counts()["jobs"] == 0


# --- `jobbot run` end to end (network mocked out) --------------------------------------


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


def test_run_without_smtp_env_fails_fast(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("SMTP_USER", raising=False)
    monkeypatch.delenv("SMTP_APP_PASSWORD", raising=False)
    monkeypatch.setattr(cli, "fetch_all", lambda *a: pytest.fail("should not fetch"))
    assert cli.main(["--db", str(tmp_path / "j.db"), "run"]) == 2
    assert "SMTP_USER" in capsys.readouterr().out


def test_run_dry_run_prints_digest_and_writes_html(fake_fetch, tmp_path, capsys):
    db, html = tmp_path / "j.db", tmp_path / "digest.html"
    assert cli.main(["--db", str(db), "run", "--dry-run", "--html", str(html)]) == 0
    out = capsys.readouterr().out
    assert "Subject: jobbot: 1 new SWE match" in out
    assert "Affirm — Software Engineer II" in out
    assert "<table" in html.read_text()
    assert not db.exists()


def test_run_sends_then_second_run_sends_nothing(fake_fetch, monkeypatch, tmp_path, capsys):
    FakeSMTP.instances = []
    monkeypatch.setattr(digest.smtplib, "SMTP_SSL", FakeSMTP)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_APP_PASSWORD", "pw")
    db = tmp_path / "j.db"

    assert cli.main(["--db", str(db), "run"]) == 0
    assert "sent digest to me@gmail.com" in capsys.readouterr().out
    assert cli.main(["--db", str(db), "run"]) == 0
    assert "no new matches; nothing sent" in capsys.readouterr().out
    assert len(FakeSMTP.instances) == 1
