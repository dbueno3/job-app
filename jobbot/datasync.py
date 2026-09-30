"""jobs.db lives on an orphan `data` branch as a single force-pushed commit.

Keeping it off master stops the repo from growing by a full DB copy on every run.
Both the bot and `jobbot sync` publish the same way:

    fetch the branch -> write jobs.db locally -> apply my changes -> push with
    --force-with-lease=<the commit I fetched>

If someone else pushed in between, the lease is rejected; we fetch their version,
re-apply our changes on top, and retry. Neither side ever overwrites the other.
"""

import json
import logging
import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from jobbot.models import utcnow
from jobbot.storage import Status, Storage

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "jobbot",
    "GIT_AUTHOR_EMAIL": "jobbot@users.noreply.github.com",
    "GIT_COMMITTER_NAME": "jobbot",
    "GIT_COMMITTER_EMAIL": "jobbot@users.noreply.github.com",
}


class GitError(Exception):
    pass


class PushConflict(Exception):
    """Still rejected after every retry (someone keeps pushing)."""


@dataclass
class PublishResult:
    outcome: str  # "pushed" | "unchanged"
    attempts: int


class DataBranch:
    def __init__(
        self,
        db_path: Path,
        repo: Path = REPO_ROOT,
        remote: str = "origin",
        branch: str = "data",
        filename: str = "jobs.db",
    ) -> None:
        self.db_path = Path(db_path)
        self.repo = Path(repo)
        self.remote = remote
        self.branch = branch
        self.filename = filename
        self.tracking_ref = f"refs/remotes/{remote}/{branch}"

    def _git(self, *args: str, input: bytes | None = None, check: bool = True):
        proc = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            input=input,
            capture_output=True,
            env={**os.environ, **BOT_IDENTITY},
        )
        if check and proc.returncode != 0:
            raise GitError(f"git {' '.join(args)}: {proc.stderr.decode().strip()}")
        return proc

    def fetch(self) -> str | None:
        """Fetch the branch and write its jobs.db to db_path. Returns the commit sha,
        or None if the branch doesn't exist yet (db_path is then removed)."""
        proc = self._git(
            "fetch", "--no-tags", self.remote, f"+refs/heads/{self.branch}:{self.tracking_ref}",
            check=False,
        )  # fmt: skip
        if proc.returncode != 0:
            if b"couldn't find remote ref" not in proc.stderr:
                raise GitError(proc.stderr.decode().strip())
            self._git("update-ref", "-d", self.tracking_ref, check=False)
            self.db_path.unlink(missing_ok=True)
            return None
        sha = self._git("rev-parse", self.tracking_ref).stdout.decode().strip()
        blob = self._git("cat-file", "blob", f"{sha}:{self.filename}").stdout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_bytes(blob)
        return sha

    def exists(self) -> bool:
        """Does the branch exist on the remote? (Asks the remote; no fetch.)"""
        out = self._git("ls-remote", "--heads", self.remote, self.branch).stdout
        return bool(out.strip())

    def _blob_of_commit(self, sha: str) -> str:
        return self._git("rev-parse", f"{sha}:{self.filename}").stdout.decode().strip()

    def _blob_of_file(self) -> str:
        return self._git("hash-object", str(self.db_path)).stdout.decode().strip()

    def push(self, expected: str | None, message: str = "Update jobs.db") -> bool:
        """Push db_path as a single root commit. False if the lease was rejected."""
        blob = self._git("hash-object", "-w", str(self.db_path)).stdout.decode().strip()
        tree_line = f"100644 blob {blob}\t{self.filename}\n".encode()
        tree = self._git("mktree", input=tree_line).stdout.decode().strip()
        commit = self._git("commit-tree", tree, "-m", message).stdout.decode().strip()
        lease = f"--force-with-lease=refs/heads/{self.branch}:{expected or ''}"
        proc = self._git(
            "push", "--porcelain", lease, self.remote, f"{commit}:refs/heads/{self.branch}",
            check=False,
        )  # fmt: skip
        if proc.returncode == 0:
            self._git("update-ref", self.tracking_ref, commit)
            return True
        output = (proc.stdout + proc.stderr).decode()
        if "stale info" in output or "rejected" in output or "fetch first" in output:
            return False
        raise GitError(output.strip())

    def publish(
        self,
        apply: Callable[[Path, int], None],
        message: str = "Update jobs.db",
        max_attempts: int = 5,
        sleep: Callable[[float], None] | None = None,
        require_existing: bool = False,
    ) -> PublishResult:
        """fetch -> apply(db_path, attempt) -> push with lease; retry on rejection.

        `apply` must be safe to run again on a newer DB (attempt > 1): it re-applies
        the same changes on top of whatever the other side pushed.
        """
        sleep = sleep or time.sleep
        for attempt in range(1, max_attempts + 1):
            base = self.fetch()
            if base is None and require_existing:
                # Never publish over a missing branch: that would replace real data
                # with whatever apply() builds from nothing.
                raise GitError(f"no {self.branch} branch yet (seed it with `jobbot sync --seed`)")
            apply(self.db_path, attempt)
            if base is not None and self._blob_of_file() == self._blob_of_commit(base):
                return PublishResult("unchanged", attempt)
            if self.push(base, message):
                return PublishResult("pushed", attempt)
            log.info("data branch moved during publish; re-applying (attempt %d)", attempt)
            sleep(min(2**attempt, 30))
        raise PushConflict(f"gave up after {max_attempts} rejected pushes")


# --- local status journal ------------------------------------------------------------


class Journal:
    """Local status changes waiting to be synced, one JSON object per line.

    Keyed by (source, source_job_id), not the integer id, and cleared only after a
    successful push, so a change is never lost to a rejected push.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def append(self, source: str, source_job_id: str, status: Status, note: str | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "op": "status",
            "source": source,
            "source_job_id": source_job_id,
            "status": str(status),
            "note": note,
            "at": utcnow().isoformat(),
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def replay(store: Storage, entries: list[dict]) -> list[str]:
    """Apply journaled status changes (with their original timestamps). Returns warnings."""
    warnings = []
    for e in entries:
        stored = store.get_by_key(e["source"], e["source_job_id"])
        if stored is None:
            warnings.append(f"skipped change for unknown job {e['source']}/{e['source_job_id']}")
            continue
        store.set_status(
            stored.id, Status(e["status"]), note=e["note"], now=datetime.fromisoformat(e["at"])
        )
    return warnings
