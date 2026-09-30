import json
import sqlite3
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from jobbot.filters import fingerprint
from jobbot.models import Job, Match, utcnow

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "jobs.db"
SCHEMA_VERSION = 2


class Status(StrEnum):
    NEW = "new"
    APPLIED = "applied"
    REACHED_OUT = "reached_out"
    INTERVIEWING = "interviewing"
    REJECTED = "rejected"
    SKIPPED = "skipped"


# A repost of a job I already acted on (or passed on) stays out of the digest.
REPOST_EXCLUDED = (
    Status.APPLIED,
    Status.REACHED_OUT,
    Status.INTERVIEWING,
    Status.REJECTED,
    Status.SKIPPED,
)

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
    description      BLOB,             -- zlib-compressed text; NULL if never fetched or pruned
    posted_at        TEXT,
    first_seen_at    TEXT NOT NULL,
    compensation     TEXT,
    matched          INTEGER NOT NULL DEFAULT 0,
    drop_reason      TEXT,             -- first filter that rejected it; NULL when matched
    score            INTEGER NOT NULL DEFAULT 0,
    min_yoe          REAL,
    matched_keywords TEXT NOT NULL DEFAULT '[]',  -- JSON list
    gaps             TEXT NOT NULL DEFAULT '[]',  -- JSON list
    fingerprint      TEXT NOT NULL,
    is_repost        INTEGER NOT NULL DEFAULT 0,
    repost_of        INTEGER REFERENCES jobs (id),
    status           TEXT NOT NULL DEFAULT 'new' CHECK (status IN ({_STATUSES})),
    notes            TEXT NOT NULL DEFAULT '',
    digested_at      TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_fingerprint ON jobs (fingerprint);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs (matched, digested_at);

CREATE TABLE IF NOT EXISTS status_history (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id  INTEGER NOT NULL REFERENCES jobs (id),
    status  TEXT NOT NULL CHECK (status IN ({_STATUSES})),
    note    TEXT,
    at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_job ON status_history (job_id);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class StoredJob(Match):
    id: int
    matched: bool
    drop_reason: str | None
    is_repost: bool
    repost_of: int | None
    status: Status
    notes: str
    digested_at: datetime | None
    updated_at: datetime


@dataclass
class Stats:
    applied_this_week: int
    by_status: dict[str, int]
    by_company: list[tuple[str, int, int]]  # (company, applied, responded)


def fingerprint_key(job: Job) -> str:
    return "\x1f".join(fingerprint(job))


def _iso(dt: datetime | None) -> str | None:
    """Store everything as UTC so ISO strings sort chronologically in SQL."""
    return dt.astimezone(UTC).isoformat() if dt else None


def compress(text: str | None) -> bytes | None:
    return zlib.compress(text.encode(), 6) if text is not None else None


def decompress(blob: bytes | None) -> str | None:
    return zlib.decompress(blob).decode() if blob is not None else None


class Storage:
    """SQLite job store: every job's metadata, dedupe, reposts, status tracking.

    Writes are not auto-committed; call commit(). Closing without committing
    discards pending writes.
    """

    def __init__(self, path: Path | str = DEFAULT_DB) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # --- schema ---------------------------------------------------------------------

    def _migrate(self) -> None:
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version == SCHEMA_VERSION:
            return
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(jobs)")}
        with self.conn:
            if "description_text" in cols:  # v1: matches only, plain-text descriptions
                self._migrate_v1()
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def _migrate_v1(self) -> None:
        """v1 stored only matched jobs, all of them already emailed.

        Keep ids, statuses, and notes. Rows become matched=1 with digested_at =
        first_seen_at so nothing is re-sent. Non-new statuses get a history entry.
        """
        self.conn.execute("ALTER TABLE jobs RENAME TO jobs_v1")
        self.conn.execute("DROP INDEX IF EXISTS idx_jobs_fingerprint")
        self.conn.execute("DROP INDEX IF EXISTS idx_jobs_status")
        self.conn.executescript(SCHEMA)
        for r in self.conn.execute("SELECT * FROM jobs_v1 ORDER BY id").fetchall():
            keywords = [k for k in r["keywords"].split(",") if k]
            self.conn.execute(
                """
                INSERT INTO jobs (id, source, source_job_id, company, title, location,
                    is_remote, url, description, posted_at, first_seen_at, matched, score,
                    min_yoe, matched_keywords, fingerprint, status, notes, digested_at,
                    updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    r["id"], r["source"], r["source_job_id"], r["company"], r["title"],
                    r["location"], r["is_remote"], r["url"], compress(r["description_text"]),
                    r["posted_at"], r["first_seen_at"], r["score"], r["min_yoe"],
                    json.dumps(keywords), r["fingerprint"], r["status"], r["notes"],
                    r["first_seen_at"], r["updated_at"],
                ),
            )  # fmt: skip
            if r["status"] != Status.NEW:
                self.conn.execute(
                    "INSERT INTO status_history (job_id, status, note, at) VALUES (?, ?, ?, ?)",
                    (r["id"], r["status"], r["notes"] or None, r["updated_at"]),
                )
        self.conn.execute("DROP TABLE jobs_v1")

    # --- ingest -----------------------------------------------------------------------

    def has(self, job: Job) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND source_job_id = ?", job.key
        ).fetchone()
        return row is not None

    def insert(
        self,
        job: Job,
        match: Match | None,
        drop_reason: str | None,
        now: datetime | None = None,
    ) -> int:
        """Store a newly seen job (matched or not). Returns its id.

        A job is a likely repost if an earlier row has the same (company, normalized
        title, location) under a different source_job_id; repost_of points at the
        earliest such row.
        """
        now = now or utcnow()
        fp = fingerprint_key(job)
        original = self.conn.execute(
            "SELECT id FROM jobs WHERE fingerprint = ? AND NOT "
            "(source = ? AND source_job_id = ?) ORDER BY id LIMIT 1",
            (fp, *job.key),
        ).fetchone()
        cur = self.conn.execute(
            """
            INSERT INTO jobs (source, source_job_id, company, title, location, is_remote,
                url, description, posted_at, first_seen_at, compensation, matched,
                drop_reason, score, min_yoe, matched_keywords, gaps, fingerprint,
                is_repost, repost_of, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.source, job.source_job_id, job.company, job.title, job.location,
                int(job.is_remote), job.url, compress(job.description_text),
                _iso(job.posted_at), _iso(job.first_seen_at), job.compensation,
                int(match is not None), drop_reason,
                match.score if match else 0, match.min_yoe if match else None,
                json.dumps(match.keywords if match else []),
                json.dumps(match.gaps if match else []),
                fp, int(original is not None), original["id"] if original else None,
                _iso(now),
            ),
        )  # fmt: skip
        return cur.lastrowid

    def update_evaluation(
        self, job_id: int, match: Match | None, drop_reason: str | None, now: datetime | None = None
    ) -> None:
        """Re-apply filters to a stored job (--refilter). Status and digest state untouched."""
        self.conn.execute(
            """
            UPDATE jobs SET matched = ?, drop_reason = ?, score = ?, min_yoe = ?,
                matched_keywords = ?, gaps = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(match is not None), drop_reason,
                match.score if match else 0, match.min_yoe if match else None,
                json.dumps(match.keywords if match else []),
                json.dumps(match.gaps if match else []),
                _iso(now or utcnow()), job_id,
            ),
        )  # fmt: skip

    def set_details(self, job_id: int, description: str | None, compensation: str | None) -> None:
        self.conn.execute(
            "UPDATE jobs SET description = ?, compensation = COALESCE(?, compensation) "
            "WHERE id = ?",
            (compress(description), compensation, job_id),
        )

    def prune_descriptions(self, now: datetime | None = None, days: int = 14) -> int:
        """Drop descriptions of unmatched jobs first seen more than `days` ago."""
        cutoff = _iso((now or utcnow()) - timedelta(days=days))
        cur = self.conn.execute(
            "UPDATE jobs SET description = NULL "
            "WHERE matched = 0 AND description IS NOT NULL AND first_seen_at < ?",
            (cutoff,),
        )
        return cur.rowcount

    # --- digest ---------------------------------------------------------------------

    def pending_digest(self) -> list[StoredJob]:
        """Matched, not yet emailed, and not a repost of something I already acted on."""
        excluded = ", ".join(f"'{s}'" for s in REPOST_EXCLUDED)
        rows = self.conn.execute(
            f"""
            SELECT j.* FROM jobs j LEFT JOIN jobs o ON o.id = j.repost_of
            WHERE j.matched = 1 AND j.digested_at IS NULL
              AND NOT (j.is_repost = 1 AND o.status IN ({excluded}))
            ORDER BY j.score DESC, j.posted_at IS NULL, j.posted_at DESC, j.id
            """
        )
        return [_from_row(r) for r in rows]

    def mark_digested(self, keys: Iterable[tuple[str, str]], at: datetime) -> None:
        self.conn.executemany(
            "UPDATE jobs SET digested_at = ? WHERE source = ? AND source_job_id = ?",
            [(_iso(at), *k) for k in keys],
        )

    # --- reads ------------------------------------------------------------------------

    def get(self, job_id: int) -> StoredJob | None:
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _from_row(row) if row else None

    def get_by_key(self, source: str, source_job_id: str) -> StoredJob | None:
        row = self.conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?", (source, source_job_id)
        ).fetchone()
        return _from_row(row) if row else None

    def all_ids(self) -> list[int]:
        return [r[0] for r in self.conn.execute("SELECT id FROM jobs ORDER BY id")]

    def list_jobs(self, status: Status | None = None, matched_only: bool = True) -> list[StoredJob]:
        """Jobs ordered by score, then most recently posted."""
        where, params = [], []
        if matched_only:
            where.append("matched = 1")
        if status:
            where.append("status = ?")
            params.append(status)
        query = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        query += " ORDER BY score DESC, posted_at IS NULL, posted_at DESC, id DESC"
        return [_from_row(r) for r in self.conn.execute(query, params)]

    def counts(self) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT COUNT(*), SUM(matched), SUM(description IS NOT NULL) FROM jobs"
        ).fetchone()
        return {"jobs": row[0], "matched": row[1] or 0, "with_description": row[2] or 0}

    # --- status tracking ----------------------------------------------------------------

    def set_status(
        self,
        job_id: int,
        status: Status,
        note: str | None = None,
        now: datetime | None = None,
    ) -> StoredJob | None:
        """Update status (and optionally replace the note), recording it in history.

        Returns None if the id is unknown. Does not commit.
        """
        now = now or utcnow()
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
        if not cur.rowcount:
            return None
        self.conn.execute(
            "INSERT INTO status_history (job_id, status, note, at) VALUES (?, ?, ?, ?)",
            (job_id, status, note, _iso(now)),
        )
        return self.get(job_id)

    def stats(self, now: datetime | None = None) -> Stats:
        """Applications this week, counts by status, and response rate by company.

        "Applied" = ever marked applied, or currently interviewing/rejected.
        "Responded" = currently interviewing or rejected.
        """
        now = now or utcnow()
        week_ago = _iso(now - timedelta(days=7))
        applied_this_week = self.conn.execute(
            "SELECT COUNT(DISTINCT job_id) FROM status_history "
            "WHERE status = 'applied' AND at >= ?",
            (week_ago,),
        ).fetchone()[0]
        by_status = {
            r[0]: r[1]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) FROM jobs WHERE matched = 1 OR status != 'new' "
                "GROUP BY status ORDER BY COUNT(*) DESC"
            )
        }
        by_company = [
            (r[0], r[1], r[2])
            for r in self.conn.execute(
                """
                SELECT company,
                    COUNT(*) AS applied,
                    SUM(status IN ('interviewing', 'rejected')) AS responded
                FROM jobs
                WHERE status IN ('interviewing', 'rejected')
                   OR id IN (SELECT job_id FROM status_history WHERE status = 'applied')
                GROUP BY company ORDER BY applied DESC, company
                """
            )
        ]
        return Stats(applied_this_week, by_status, by_company)

    # --- meta -------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def _from_row(row: sqlite3.Row) -> StoredJob:
    job = Job(
        source=row["source"],
        source_job_id=row["source_job_id"],
        company=row["company"],
        title=row["title"],
        location=row["location"],
        is_remote=bool(row["is_remote"]),
        url=row["url"],
        description_text=decompress(row["description"]),
        posted_at=row["posted_at"],
        first_seen_at=row["first_seen_at"],
        compensation=row["compensation"],
    )
    return StoredJob(
        id=row["id"],
        job=job,
        score=row["score"],
        min_yoe=row["min_yoe"],
        keywords=json.loads(row["matched_keywords"]),
        gaps=json.loads(row["gaps"]),
        matched=bool(row["matched"]),
        drop_reason=row["drop_reason"],
        is_repost=bool(row["is_repost"]),
        repost_of=row["repost_of"],
        status=row["status"],
        notes=row["notes"],
        digested_at=row["digested_at"],
        updated_at=row["updated_at"],
    )
