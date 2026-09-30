import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from jobbot.models import Match
from jobbot.storage import Status, Storage, compress, decompress
from tests.test_filters import make_job

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def match(score=1, min_yoe=None, keywords=(), gaps=(), **job_fields) -> Match:
    return Match(
        job=make_job(**job_fields),
        score=score,
        min_yoe=min_yoe,
        keywords=list(keywords),
        gaps=list(gaps),
    )


@pytest.fixture
def store():
    with Storage(":memory:") as s:
        yield s


def add(store, m: Match | None = None, drop_reason=None, **job_fields) -> int:
    """Insert a matched job (m) or an unmatched one (job_fields + drop_reason)."""
    if m is not None:
        return store.insert(m.job, m, None, NOW)
    return store.insert(make_job(**job_fields), None, drop_reason or "title", NOW)


# --- insert / read ----------------------------------------------------------------


def test_matched_job_round_trips(store):
    m = match(
        score=4, min_yoe=2.5, keywords=["Go", "payments"], gaps=["Java"], is_remote=True,
        description_text="Build payments in Go.", compensation="$150K – $200K",
    )  # fmt: skip
    stored = store.get(add(store, m))
    assert stored.job == m.job  # every model field survives, description included
    assert (stored.score, stored.min_yoe, stored.keywords, stored.gaps) == (
        4, 2.5, ["Go", "payments"], ["Java"],
    )  # fmt: skip
    assert stored.matched and stored.drop_reason is None
    assert stored.status == Status.NEW and stored.digested_at is None and not stored.is_repost


def test_unmatched_job_stores_metadata_and_reason(store):
    stored = store.get(add(store, title="Account Executive", description_text=None))
    assert not stored.matched and stored.drop_reason == "title"
    assert stored.job.description_text is None and stored.keywords == [] and stored.score == 0


def test_descriptions_are_compressed(store):
    text = "Python " * 1000
    job_id = add(store, match(description_text=text))
    blob = store.conn.execute("SELECT description FROM jobs WHERE id = ?", (job_id,)).fetchone()[0]
    assert len(blob) < len(text) / 10
    assert decompress(blob) == text and compress(None) is None


def test_has_and_unique_key(store):
    add(store, match(source_job_id="1"))
    assert store.has(make_job(source_job_id="1"))
    assert not store.has(make_job(source_job_id="2"))
    assert not store.has(make_job(source="ashby", source_job_id="1"))
    with pytest.raises(sqlite3.IntegrityError):
        add(store, match(source_job_id="1"))


# --- reposts ------------------------------------------------------------------------


def test_repost_detected_by_company_title_location(store):
    original = add(store, match(source_job_id="1"))
    repost = store.get(add(store, match(source_job_id="2", title=" software  ENGINEER ")))
    assert repost.is_repost and repost.repost_of == original


def test_same_title_elsewhere_is_not_a_repost(store):
    add(store, match(source_job_id="1"))
    other_city = store.get(add(store, match(source_job_id="2", location="US-Remote")))
    other_company = store.get(add(store, match(source_job_id="3", company="Other")))
    assert not other_city.is_repost and not other_company.is_repost


def test_repost_points_at_earliest_original(store):
    first = add(store, match(source_job_id="1"))
    add(store, match(source_job_id="2"))
    assert store.get(add(store, match(source_job_id="3"))).repost_of == first


@pytest.mark.parametrize(
    "original_status, in_digest",
    [
        (Status.NEW, True),
        (Status.APPLIED, False),
        (Status.REACHED_OUT, False),
        (Status.INTERVIEWING, False),
        (Status.REJECTED, False),
        (Status.SKIPPED, False),
    ],
)
def test_repost_excluded_from_digest_if_original_was_acted_on(store, original_status, in_digest):
    original = add(store, match(source_job_id="1"))
    store.mark_digested([("greenhouse", "1")], NOW)
    store.set_status(original, original_status)
    add(store, match(source_job_id="2"))
    assert [s.job.source_job_id for s in store.pending_digest()] == (["2"] if in_digest else [])


# --- digest state -------------------------------------------------------------------


def test_pending_digest_is_matched_and_undigested_sorted_by_score(store):
    add(store, match(score=1, source_job_id="low", title="A Engineer"))
    add(store, match(score=5, source_job_id="high", title="B Engineer"))
    add(store, source_job_id="unmatched", title="Account Executive")
    assert [s.job.source_job_id for s in store.pending_digest()] == ["high", "low"]
    store.mark_digested([("greenhouse", "high")], NOW)
    [left] = store.pending_digest()
    assert left.job.source_job_id == "low"
    assert store.get_by_key("greenhouse", "high").digested_at == NOW


# --- status + history ---------------------------------------------------------------


def test_set_status_records_history_and_keeps_note(store):
    job_id = add(store, match())
    later = NOW + timedelta(days=1)
    updated = store.set_status(job_id, Status.REACHED_OUT, note="pinged Jane", now=later)
    assert (updated.status, updated.notes, updated.updated_at) == (
        Status.REACHED_OUT, "pinged Jane", later,
    )  # fmt: skip
    assert store.set_status(job_id, Status.INTERVIEWING).notes == "pinged Jane"
    history = store.conn.execute("SELECT status FROM status_history ORDER BY id").fetchall()
    assert [h[0] for h in history] == ["reached_out", "interviewing"]


def test_set_status_unknown_id(store):
    assert store.set_status(42, Status.APPLIED) is None
    assert store.conn.execute("SELECT COUNT(*) FROM status_history").fetchone()[0] == 0


def test_invalid_status_rejected_by_db(store):
    job_id = add(store, match())
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("UPDATE jobs SET status = 'bogus' WHERE id = ?", (job_id,))


def test_list_jobs_matched_only_by_default(store):
    add(store, match(score=1, source_job_id="a", title="A Engineer II"))
    add(store, match(score=5, source_job_id="b", title="B Engineer II"))
    add(store, source_job_id="c", title="Account Executive")
    assert [s.job.source_job_id for s in store.list_jobs()] == ["b", "a"]
    assert len(store.list_jobs(matched_only=False)) == 3
    store.set_status(1, Status.SKIPPED)
    assert [s.job.source_job_id for s in store.list_jobs(Status.SKIPPED)] == ["a"]


def test_list_sorts_across_timezone_offsets(store):
    # 10:00 -04:00 is 14:00 UTC, later than 13:00 +00:00.
    ny = datetime.fromisoformat("2026-09-20T10:00:00-04:00")
    utc = datetime.fromisoformat("2026-09-20T13:00:00+00:00")
    add(store, match(source_job_id="utc", title="A Engineer", posted_at=utc))
    add(store, match(source_job_id="ny", title="B Engineer", posted_at=ny))
    assert [s.job.source_job_id for s in store.list_jobs()] == ["ny", "utc"]


# --- pruning ------------------------------------------------------------------------


def test_prune_drops_old_unmatched_descriptions_only(store):
    old = NOW - timedelta(days=20)
    store.insert(make_job(source_job_id="old-unmatched", first_seen_at=old), None, "yoe", NOW)
    store.insert(make_job(source_job_id="new-unmatched", title="B Engineer"), None, "yoe", NOW)
    m = match(source_job_id="old-matched", title="C Engineer", first_seen_at=old)
    store.insert(m.job, m, None, NOW)
    assert store.prune_descriptions(NOW, days=14) == 1
    assert store.get_by_key("greenhouse", "old-unmatched").job.description_text is None
    assert store.get_by_key("greenhouse", "new-unmatched").job.description_text is not None
    assert store.get_by_key("greenhouse", "old-matched").job.description_text is not None


# --- stats --------------------------------------------------------------------------


def test_stats(store):
    ids = [
        add(store, match(source_job_id=str(i), company=c, title=f"E{i} Engineer"))
        for i, c in enumerate(["Stripe", "Stripe", "Ramp", "Ramp"])
    ]
    store.set_status(ids[0], Status.APPLIED, now=NOW - timedelta(days=2))
    store.set_status(ids[0], Status.INTERVIEWING, now=NOW - timedelta(days=1))
    store.set_status(ids[1], Status.APPLIED, now=NOW - timedelta(days=10))  # last week
    store.set_status(ids[2], Status.APPLIED, now=NOW - timedelta(days=1))
    store.set_status(ids[3], Status.SKIPPED, now=NOW)
    stats = store.stats(NOW)
    assert stats.applied_this_week == 2
    assert stats.by_status == {"applied": 2, "interviewing": 1, "skipped": 1}
    assert stats.by_company == [("Stripe", 2, 1), ("Ramp", 1, 0)]


def test_meta(store):
    assert store.get_meta("last_digest_at") is None
    store.set_meta("last_digest_at", "x")
    store.set_meta("last_digest_at", "y")
    assert store.get_meta("last_digest_at") == "y"


# --- persistence + migration ---------------------------------------------------------


def test_uncommitted_writes_are_discarded(tmp_path):
    path = tmp_path / "jobs.db"
    with Storage(path) as s:
        add(s, match())
    with Storage(path) as s:
        assert s.list_jobs() == []
    with Storage(path) as s:
        add(s, match())
        s.commit()
    with Storage(path) as s:
        assert len(s.list_jobs()) == 1


V1_SCHEMA = """
CREATE TABLE jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, source_job_id TEXT NOT NULL,
    company TEXT NOT NULL, title TEXT NOT NULL, location TEXT NOT NULL,
    is_remote INTEGER NOT NULL, url TEXT NOT NULL, description_text TEXT NOT NULL,
    posted_at TEXT, first_seen_at TEXT NOT NULL, score INTEGER NOT NULL, min_yoe REAL,
    keywords TEXT NOT NULL DEFAULT '', fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new', notes TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL, UNIQUE (source, source_job_id)
);
CREATE INDEX idx_jobs_fingerprint ON jobs (fingerprint);
CREATE INDEX idx_jobs_status ON jobs (status);
"""


def test_migrates_v1_database(tmp_path):
    path = tmp_path / "jobs.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    row = (
        "greenhouse", "13", "Yext", "Software Engineer", "New York, NY", 0, "https://y/13",
        "Build things in Go.", "2026-09-24T12:00:00+00:00", "2026-09-25T03:55:00+00:00", 3, 1.0,
        "Go,Python", "yext\x1fsoftware engineer\x1fnew york, ny",
    )  # fmt: skip
    conn.execute(
        "INSERT INTO jobs (id, source, source_job_id, company, title, location, is_remote, url, "
        "description_text, posted_at, first_seen_at, score, min_yoe, keywords, fingerprint, "
        "status, notes, updated_at) VALUES (13, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "'new', '', '2026-09-25T03:55:00+00:00')",
        row,
    )
    conn.execute(
        "INSERT INTO jobs (id, source, source_job_id, company, title, location, is_remote, url, "
        "description_text, first_seen_at, score, keywords, fingerprint, status, notes, "
        "updated_at) VALUES (2, 'greenhouse', '2', 'Stripe', 'SWE, Early Career', 'NYC', 0, "
        "'https://s/2', 'x', '2026-09-24T00:00:00+00:00', 1, '', 'fp2', 'skipped', "
        "'new-grad role', '2026-09-26T00:00:00+00:00')"
    )
    conn.commit()
    conn.close()

    with Storage(path) as s:
        yext = s.get(13)
        assert yext.job.company == "Yext" and yext.keywords == ["Go", "Python"]
        assert yext.job.description_text == "Build things in Go."
        assert yext.matched and yext.digested_at == yext.job.first_seen_at  # never re-sent
        skipped = s.get(2)
        assert (skipped.status, skipped.notes) == (Status.SKIPPED, "new-grad role")
        history = s.conn.execute("SELECT job_id, status, note FROM status_history").fetchall()
        assert [tuple(h) for h in history] == [(2, "skipped", "new-grad role")]
        assert s.pending_digest() == []
        assert s.conn.execute("PRAGMA user_version").fetchone()[0] == 2
        # New ids continue after the migrated ones.
        assert add(s, match(source_job_id="new", title="New Engineer")) == 14
        s.commit()
    with Storage(path) as s:  # re-open: no second migration
        assert s.counts()["jobs"] == 3
