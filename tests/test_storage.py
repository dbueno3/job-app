import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobbot.cli import main
from jobbot.models import Match
from jobbot.storage import Status, Storage
from tests.test_filters import make_job

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def match(score=1, min_yoe=None, keywords=(), **job_fields) -> Match:
    return Match(job=make_job(**job_fields), score=score, min_yoe=min_yoe, keywords=list(keywords))


@pytest.fixture
def store():
    with Storage(":memory:") as s:
        yield s


def test_add_new_inserts_and_round_trips(store):
    m = match(score=4, min_yoe=2.5, keywords=["Go", "payments"], is_remote=True)
    [stored] = store.add_new([m], now=NOW)
    assert stored.id == 1
    assert stored.status == Status.NEW
    assert stored.notes == ""
    assert stored.score == 4
    assert stored.min_yoe == 2.5
    assert stored.keywords == ["Go", "payments"]
    assert stored.job == m.job  # every model field survives the round trip
    assert stored.updated_at == NOW


def test_add_new_skips_jobs_already_stored(store):
    first = store.add_new([match(source_job_id="1")])
    again = store.add_new([match(source_job_id="1")])
    assert len(first) == 1
    assert again == []
    assert len(store.list_jobs()) == 1


def test_add_new_skips_reposts_with_new_id(store):
    store.add_new([match(source_job_id="1")])
    repost = match(source_job_id="999", title=" software  ENGINEER ")  # same fingerprint
    assert store.add_new([repost]) == []


def test_add_new_returns_only_the_new_ones_in_input_order(store):
    store.add_new([match(source_job_id="old", title="Backend Engineer")])
    batch = [
        match(score=5, source_job_id="a", title="Platform Engineer"),
        match(score=3, source_job_id="old", title="Backend Engineer"),
        match(score=1, source_job_id="b", title="Software Engineer II"),
    ]
    new = store.add_new(batch)
    assert [s.job.source_job_id for s in new] == ["a", "b"]


def test_same_id_different_sources_are_distinct(store):
    a = match(source="greenhouse", source_job_id="1", company="A")
    b = match(source="ashby", source_job_id="1", company="B")
    assert len(store.add_new([a, b])) == 2


def test_status_survives_later_runs(store):
    [stored] = store.add_new([match()])
    store.set_status(stored.id, Status.APPLIED)
    store.add_new([match()])  # the same job comes back in the next run
    assert store.get(stored.id).status == Status.APPLIED


def test_set_status_and_note(store):
    [stored] = store.add_new([match()], now=NOW)
    later = NOW + timedelta(days=1)
    updated = store.set_status(stored.id, Status.REACHED_OUT, note="pinged Jane", now=later)
    assert updated.status == Status.REACHED_OUT
    assert updated.notes == "pinged Jane"
    assert updated.updated_at == later
    # Without --note, the existing note is kept.
    updated = store.set_status(stored.id, Status.INTERVIEWING)
    assert updated.notes == "pinged Jane"


def test_set_status_unknown_id(store):
    assert store.set_status(42, Status.APPLIED) is None


def test_invalid_status_rejected_by_db(store):
    [stored] = store.add_new([match()])
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE jobs SET status = 'bogus' WHERE id = ?", (stored.id,))


def test_list_filters_by_status_and_sorts(store):
    store.add_new(
        [
            match(score=1, source_job_id="low", title="A Engineer II"),
            match(
                score=5, source_job_id="old", title="B Engineer II", posted_at=NOW - timedelta(3)
            ),
            match(score=5, source_job_id="new", title="C Engineer II", posted_at=NOW),
        ]
    )
    assert [s.job.source_job_id for s in store.list_jobs()] == ["new", "old", "low"]
    store.set_status(1, Status.SKIPPED)
    assert [s.job.source_job_id for s in store.list_jobs(Status.NEW)] == ["new", "old"]
    assert [s.job.source_job_id for s in store.list_jobs(Status.SKIPPED)] == ["low"]


def test_list_sorts_across_timezone_offsets(store):
    # 10:00 -04:00 is 14:00 UTC, which is later than 13:00 +00:00.
    ny = datetime.fromisoformat("2026-09-20T10:00:00-04:00")
    utc = datetime.fromisoformat("2026-09-20T13:00:00+00:00")
    store.add_new(
        [
            match(source_job_id="utc", title="A Engineer", posted_at=utc),
            match(source_job_id="ny", title="B Engineer", posted_at=ny),
        ]
    )
    assert [s.job.source_job_id for s in store.list_jobs()] == ["ny", "utc"]


def test_persists_to_disk(tmp_path):
    path = tmp_path / "sub" / "jobs.db"
    with Storage(path) as s:
        s.add_new([match()])
        s.commit()
    with Storage(path) as s:
        assert len(s.list_jobs()) == 1


def test_uncommitted_adds_are_discarded(tmp_path):
    path = tmp_path / "jobs.db"
    with Storage(path) as s:
        s.add_new([match()])  # no commit, e.g. the email failed
    with Storage(path) as s:
        assert s.list_jobs() == []


# --- CLI ----------------------------------------------------------------------


def test_cli_status_and_list(tmp_path, capsys):
    db = tmp_path / "jobs.db"
    with Storage(db) as s:
        s.add_new([match(title="Software Engineer II", company="Affirm")])
        s.commit()

    assert main(["--db", str(db), "status", "1", "applied", "--note", "referral"]) == 0
    assert main(["--db", str(db), "list", "--status", "applied"]) == 0
    out = capsys.readouterr().out
    assert "Affirm: Software Engineer II" in out
    assert "note: referral" in out

    assert main(["--db", str(db), "list", "--status", "new"]) == 0
    assert "(none)" in capsys.readouterr().out


def test_cli_status_unknown_id(tmp_path, capsys):
    assert main(["--db", str(tmp_path / "jobs.db"), "status", "7", "applied"]) == 1
    assert "no job with id 7" in capsys.readouterr().out


def test_cli_rejects_invalid_status(tmp_path):
    with pytest.raises(SystemExit):
        main(["--db", str(tmp_path / "jobs.db"), "status", "1", "ghosted"])
