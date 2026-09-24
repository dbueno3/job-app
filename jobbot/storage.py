import sqlite3
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from jobbot.filters import fingerprint
from jobbot.models import Job, Match, utcnow

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"


class Status(StrEnum):
    NEW = "new"
    APPLIED = "applied"
    REACHED_OUT = "reached_out"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    SKIPPED = "skipped"


_STATUSES = ", ".join(f"'{s}'" for s in Status)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT NOT NULL,
    source_job_id    TEXT NOT NULL,
    company          TEXT NOT NULL,
    title            TEXT NOT NULL,
    location         TEXT NOT NULL,
    is_remote        INTEGER NOT NULL,
    url              TEXT NOT NULL,
    description_text TEXT NOT NULL,
    posted_at        TEXT,
    first_seen_at    TEXT NOT NULL,
    score            INTEGER NOT NULL,
    min_yoe          REAL,
    keywords         TEXT NOT NULL DEFAULT '',
    fingerprint      TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUSES})),
    notes            TEXT NOT NULL DEFAULT '',
    updated_at       TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs (fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


class StoredJob(Match):
    id: int
    status: Status
    notes: str
    updated_at: datetime


def _fingerprint_key(job: Job) -> str:
    return "\x1f".join(fingerprint(job))


def _iso(dt: datetime | None) -> str | None:
    """Store everything as UTC so ISO strings sort chronologically in SQL."""
    return dt.astimezone(UTC).isoformat() if dt else None


class Storage:
    """SQLite-backed job store: cross-run dedupe plus application status tracking."""

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _seen(self, job: Job) -> bool:
        """Already stored under the same ID, or as a repost with the same fingerprint."""
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE (source = ? AND source_job_id = ?) OR fingerprint = ?",
            (job.source, job.source_job_id, _fingerprint_key(job)),
        ).fetchone()
        return row is not None

    def add_new(self, matches: list[Match], now: datetime | None = None) -> list[StoredJob]:
        """Insert matches not seen before; return only the newly inserted ones (for the digest).

        Order of the input is preserved, so sorted matches come back sorted.
        """
        now = now or utcnow()
        inserted_ids = []
        with self.conn:  # one transaction for the whole batch
            for m in matches:
                if self._seen(m.job):
                    continue
                j = m.job
                cur = self.conn.execute(
                    """
                    INSERT INTO jobs (source, source_job_id, company, title, location,
                        is_remote, url, description_text, posted_at, first_seen_at,
                        score, min_yoe, keywords, fingerprint, status, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        j.source,
                        j.source_job_id,
                        j.company,
                        j.title,
                        j.location,
                        int(j.is_remote),
                        j.url,
                        j.description_text,
                        _iso(j.posted_at),
                        _iso(j.first_seen_at),
                        m.score,
                        m.min_yoe,
                        ",".join(m.keywords),
                        _fingerprint_key(j),
                        Status.NEW,
                        _iso(now),
                    ),
                )
                inserted_ids.append(cur.lastrowid)
        return [self.get(i) for i in inserted_ids]

    def get(self, job_id: int) -> StoredJob | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _from_row(row) if row else None

    def list_jobs(self, status: Status | None = None) -> list[StoredJob]:
        """Jobs ordered by score, then most recently posted."""
        query = "SELECT * FROM jobs"
        params: tuple = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY score DESC, posted_at IS NULL, posted_at DESC, id DESC"
        return [_from_row(r) for r in self.conn.execute(query, params)]

    def set_status(
        self, job_id: int, status: Status, note: str | None = None, now: datetime | None = None
    ) -> StoredJob | None:
        """Update a job's status (and optionally replace its note). None if the id is unknown."""
        now = now or utcnow()
        with self.conn:
            if note is None:
                cur = self.conn.execute(
                    "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                    (status, _iso(now), job_id),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE jobs SET status = ?, notes = ?, updated_at = ? WHERE id = ?",
                    (status, note, _iso(now), job_id),
                )
        return self.get(job_id) if cur.rowcount else None


def _from_row(row: sqlite3.Row) -> StoredJob:
    job = Job(
        source=row["source"],
        source_job_id=row["source_job_id"],
        company=row["company"],
        title=row["title"],
        location=row["location"],
        is_remote=bool(row["is_remote"]),
        url=row["url"],
        description_text=row["description_text"],
        posted_at=row["posted_at"],
        first_seen_at=row["first_seen_at"],
    )
    return StoredJob(
        id=row["id"],
        job=job,
        score=row["score"],
        min_yoe=row["min_yoe"],
        keywords=[k for k in row["keywords"].split(",") if k],
        status=row["status"],
        notes=row["notes"],
        updated_at=row["updated_at"],
    )
