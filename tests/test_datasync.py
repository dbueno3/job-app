import subprocess
from datetime import datetime
from pathlib import Path

import pytest

from jobbot.cli import main
from jobbot.datasync import DataBranch, GitError, Journal, PushConflict, replay
from jobbot.storage import Status, Storage
from tests.test_storage import match


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def remote(tmp_path) -> Path:
    path = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(path))
    return path


def clone(tmp_path: Path, remote: Path, name: str) -> Path:
    """A checkout with `origin` pointing at the bare remote (a laptop, or the bot)."""
    path = tmp_path / name
    path.mkdir()
    git(path, "init", "-q")
    git(path, "remote", "add", "origin", str(remote))
    return path


def branch_for(repo: Path) -> DataBranch:
    return DataBranch(repo / "data" / "jobs.db", repo=repo)


def seed(repo: Path, *matches) -> DataBranch:
    branch = branch_for(repo)
    with Storage(branch.db_path) as s:
        for m in matches:
            s.insert(m.job, m, None)
        s.commit()
    assert branch.push(expected=None, message="Seed jobs.db")
    return branch


def statuses(branch: DataBranch) -> dict[str, str]:
    branch.fetch()
    with Storage(branch.db_path) as s:
        return {r.job.source_job_id: r.status for r in s.list_jobs()}


def set_status(source_job_id: str, status: Status):
    def apply(path: Path, attempt: int) -> None:
        with Storage(path) as s:
            s.set_status(s.get_by_key("greenhouse", source_job_id).id, status)
            s.commit()

    return apply


JOBS = [match(source_job_id="1", title="A Engineer"), match(source_job_id="2", title="B Engineer")]


def test_fetch_missing_branch(tmp_path, remote):
    branch = branch_for(clone(tmp_path, remote, "a"))
    branch.db_path.parent.mkdir()
    branch.db_path.write_text("stale")
    assert branch.fetch() is None
    assert not branch.db_path.exists()


def test_seed_then_fetch_elsewhere(tmp_path, remote):
    seed(clone(tmp_path, remote, "a"), *JOBS)
    b = branch_for(clone(tmp_path, remote, "b"))
    assert b.fetch() is not None
    assert statuses(b) == {"1": "new", "2": "new"}


def test_seed_never_overwrites_existing_branch(tmp_path, remote):
    seed(clone(tmp_path, remote, "a"), *JOBS)
    other = branch_for(clone(tmp_path, remote, "b"))
    with Storage(other.db_path) as s:
        s.commit()  # an empty DB
    assert other.push(expected=None) is False
    assert statuses(other) == {"1": "new", "2": "new"}  # still the real data


def test_branch_is_a_single_commit(tmp_path, remote):
    a = seed(clone(tmp_path, remote, "a"), *JOBS)
    a.publish(set_status("1", Status.APPLIED))
    a.publish(set_status("2", Status.SKIPPED))
    count = git(remote, "rev-list", "--count", "refs/heads/data")
    files = git(remote, "ls-tree", "--name-only", "refs/heads/data")
    assert (count, files) == ("1", "jobs.db")  # no history accumulates


def test_publish_unchanged_pushes_nothing(tmp_path, remote):
    a = seed(clone(tmp_path, remote, "a"), *JOBS)
    before = git(remote, "rev-parse", "refs/heads/data")
    result = a.publish(lambda path, attempt: None)
    assert result.outcome == "unchanged"
    assert git(remote, "rev-parse", "refs/heads/data") == before


def test_publish_requires_existing_branch(tmp_path, remote):
    a = branch_for(clone(tmp_path, remote, "a"))
    with pytest.raises(GitError, match="no data branch"):
        a.publish(lambda path, attempt: None, require_existing=True)
    assert git(remote, "branch", "--list") == ""


def test_lease_rejection_reapplies_on_top_of_other_side(tmp_path, remote):
    """The laptop pushes while the bot is mid-run: the bot re-applies; nothing is lost."""
    bot = seed(clone(tmp_path, remote, "bot"), *JOBS)
    laptop = branch_for(clone(tmp_path, remote, "laptop"))
    attempts = []

    def bot_change(path: Path, attempt: int) -> None:
        attempts.append(attempt)
        if attempt == 1:  # someone else pushes after our fetch, before our push
            laptop.publish(set_status("1", Status.APPLIED))
        set_status("2", Status.SKIPPED)(path, attempt)

    result = bot.publish(bot_change)
    assert (result.outcome, result.attempts, attempts) == ("pushed", 2, [1, 2])
    assert statuses(bot) == {"1": "applied", "2": "skipped"}  # both changes survived


def test_gives_up_after_max_attempts(tmp_path, remote):
    bot = seed(clone(tmp_path, remote, "bot"), *JOBS)
    laptop = branch_for(clone(tmp_path, remote, "laptop"))
    flip = iter([Status.APPLIED, Status.SKIPPED, Status.REJECTED])

    def always_conflict(path: Path, attempt: int) -> None:
        laptop.publish(set_status("1", next(flip)))
        set_status("2", Status.SKIPPED)(path, attempt)

    with pytest.raises(PushConflict):
        bot.publish(always_conflict, max_attempts=3)


# --- journal ------------------------------------------------------------------------


def test_journal_replay_by_key_with_original_time(tmp_path):
    journal = Journal(tmp_path / "pending.jsonl")
    journal.append("greenhouse", "1", Status.APPLIED, "referral")
    journal.append("greenhouse", "999", Status.SKIPPED, None)
    entries = journal.entries()
    with Storage(":memory:") as s:
        for m in JOBS:
            s.insert(m.job, m, None)
        warnings = replay(s, entries)
        row = s.get_by_key("greenhouse", "1")
        assert (row.status, row.notes) == (Status.APPLIED, "referral")
        assert row.updated_at == datetime.fromisoformat(entries[0]["at"])
    assert warnings == ["skipped change for unknown job greenhouse/999"]
    journal.clear()
    assert journal.entries() == []


# --- CLI: status + sync -------------------------------------------------------------


def cli(repo: Path, *args: str) -> int:
    return main(["--db", str(repo / "data" / "jobs.db"), "--repo", str(repo), *args])


def test_status_then_sync_survives_a_concurrent_bot_push(tmp_path, remote, capsys):
    bot = seed(clone(tmp_path, remote, "bot"), *JOBS)
    laptop = clone(tmp_path, remote, "laptop")
    assert cli(laptop, "sync") == 0  # pull
    assert cli(laptop, "status", "1", "applied", "--note", "referral") == 0
    assert "1 local change(s) not synced yet" in capsys.readouterr().out

    bot.publish(set_status("2", Status.SKIPPED))  # the bot pushes before I sync

    assert cli(laptop, "sync") == 0
    assert "pushed 1 change(s)" in capsys.readouterr().out
    assert statuses(bot) == {"1": "applied", "2": "skipped"}
    assert not (laptop / "data" / "pending-changes.jsonl").exists()


def test_sync_refuses_without_data_branch(tmp_path, remote, capsys):
    laptop = clone(tmp_path, remote, "laptop")
    assert cli(laptop, "sync") == 1
    assert "no data branch" in capsys.readouterr().out


def test_sync_seed(tmp_path, remote, capsys):
    laptop = clone(tmp_path, remote, "laptop")
    with Storage(laptop / "data" / "jobs.db") as s:
        for m in JOBS:
            s.insert(m.job, m, None)
        s.commit()
    assert cli(laptop, "sync", "--seed") == 0
    assert cli(laptop, "sync", "--seed") == 1  # second time: branch exists, refuse
    assert "already exists" in capsys.readouterr().out
    assert statuses(branch_for(clone(tmp_path, remote, "other"))) == {"1": "new", "2": "new"}


def test_status_without_local_db(tmp_path, remote, capsys):
    laptop = clone(tmp_path, remote, "laptop")
    assert cli(laptop, "status", "1", "applied") == 1
    assert "run `jobbot sync` first" in capsys.readouterr().out
