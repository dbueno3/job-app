from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from jobbot import cli, digest
from jobbot.cli import store_and_notify
from jobbot.digest import (
    DigestConfigError,
    SmtpConfig,
    build_digest,
    recruiter_search_url,
    send_digest,
)
from jobbot.sources import FetchResult
from jobbot.storage import Storage
from tests.test_storage import match

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


@pytest.fixture
def store():
    with Storage(":memory:") as s:
        yield s


def stored(store, *matches):
    new = store.add_new(list(matches))
    store.commit()
    return new


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


# --- store_and_notify: commit only after a successful send ---------------------------


def test_commits_after_successful_notify(store):
    sent = []
    new = store_and_notify(store, [match()], sent.append)
    assert len(new) == 1 and sent == [new]
    store.rollback()  # nothing pending: it was already committed
    assert len(store.list_jobs()) == 1


def test_failed_notify_rolls_back_so_jobs_are_retried(store):
    def boom(new):
        raise OSError("smtp down")

    with pytest.raises(OSError):
        store_and_notify(store, [match()], boom)
    assert store.list_jobs() == []
    # Next run: same job is still "new" and gets sent.
    sent = []
    store_and_notify(store, [match()], sent.append)
    assert len(sent[0]) == 1


def test_dry_run_notifies_but_saves_nothing(store):
    sent = []
    new = store_and_notify(store, [match()], sent.append, dry_run=True)
    assert len(new) == 1 and len(sent) == 1
    assert store.list_jobs() == []


def test_no_new_matches_sends_nothing(store):
    stored(store, match())
    sent = []
    assert store_and_notify(store, [match()], sent.append) == []
    assert sent == []


# --- `jobbot run` end to end (network mocked out) --------------------------------------


@pytest.fixture
def fake_fetch(monkeypatch):
    job = match(title="Software Engineer II", company="Affirm", posted_at=datetime.now(UTC)).job
    monkeypatch.setattr(cli, "fetch_all", lambda companies, client: FetchResult(jobs=[job]))


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
    with Storage(db) as s:
        assert s.list_jobs() == []


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
